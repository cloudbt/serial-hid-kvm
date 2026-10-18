"""Mock kvm-ai-hub: same port serves the HTTP job API and the captions WS.

Implements the API contract used by ``_captions.py`` / ``_minutes.py``:

    GET    /health
    WS     /v1/captions            start -> ready, PCM -> caption lines, stop
    POST   /v1/jobs/transcribe     multipart file=FLAC, language
    GET    /v1/jobs/{id}           status + progress
    GET    /v1/jobs/{id}/result    segments
    DELETE /v1/jobs/{id}

Captions come from a crude energy VAD (a sentence ends after 0.5 s of
silence) or, with ``caption_every``, every N seconds of received audio.
Transcripts are synthetic: one Japanese segment per ``segment_seconds`` of
FLAC audio (length read from the STREAMINFO block).

Run standalone for manual end-to-end checks::

    python -m tests.mock_ai_hub --port 8765 --key devkey --caption-every 3
"""
import argparse
import base64
import email.parser
import email.policy
import hashlib
import json
import math
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def flac_seconds(data):
    if data[:4] != b"fLaC" or len(data) < 42:
        raise ValueError("not FLAC")
    info = data[8:8 + 34]
    packed = int.from_bytes(info[10:18], "big")
    rate = packed >> 44
    total = packed & ((1 << 36) - 1)
    return total / rate if rate else 0.0


class MockHub:
    def __init__(self, key=None, caption_every=None, job_seconds=1.0, segment_seconds=10.0,
                 fail_jobs=False, host="127.0.0.1", port=0):
        self.key = key
        self.caption_every = caption_every
        self.job_seconds = job_seconds
        self.segment_seconds = segment_seconds
        self.fail_jobs = fail_jobs
        self.jobs = {}
        self.uploads = []
        self.caption_sessions = []
        self.lock = threading.Lock()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _authorized(self):
                if not hub.key:
                    return True
                if self.headers.get("Authorization") == "Bearer " + hub.key:
                    return True
                return parse_qs(urlsplit(self.path).query).get("token") == [hub.key]

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/health":
                    return self._json(200, {"status": "ok", "version": "mock", "caption_model": "mock-asr",
                                            "batch_model": "mock-asr", "busy": {"jobs": len(hub.jobs)}})
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                if path == "/v1/captions":
                    return hub._captions(self)
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["v1", "jobs"]:
                    job = hub._job(parts[2])
                    if job is None:
                        return self._json(404, {"error": "no such job"})
                    if len(parts) == 4 and parts[3] == "result":
                        if job["status"] != "done":
                            return self._json(409, {"error": "not done"})
                        return self._json(200, job["result"])
                    return self._json(200, {k: job[k] for k in ("status", "progress", "error")})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    return self._json(401, {"error": "unauthorized"})
                if urlsplit(self.path).path != "/v1/jobs/transcribe":
                    return self._json(404, {"error": "not found"})
                body = self.rfile.read(int(self.headers["Content-Length"]))
                message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True)
                try:
                    seconds = flac_seconds(fields["file"])
                except (KeyError, ValueError):
                    return self._json(400, {"error": "file must be FLAC"})
                job_id = uuid.uuid4().hex
                hub.uploads.append({"bytes": len(fields["file"]), "language": fields.get("language", b"").decode(),
                                    "seconds": seconds})
                with hub.lock:
                    hub.jobs[job_id] = {"created": time.monotonic(), "seconds": seconds, "status": "queued",
                                        "progress": {"done": 0, "total": max(1, math.ceil(seconds / 30))},
                                        "error": None, "result": None}
                self._json(200, {"job_id": job_id})

            def do_DELETE(self):
                if not self._authorized():
                    return self._json(401, {"error": "unauthorized"})
                parts = urlsplit(self.path).path.strip("/").split("/")
                with hub.lock:
                    hub.jobs.pop(parts[-1], None)
                self._json(200, {"deleted": True})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self.thread = None

    # -- jobs ----------------------------------------------------------------
    def _job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            elapsed = time.monotonic() - job["created"]
            total = job["progress"]["total"]
            if job["status"] not in ("done", "failed"):
                fraction = min(1.0, elapsed / max(self.job_seconds, 1e-6))
                job["status"] = "running" if fraction < 1 else "done"
                job["progress"]["done"] = int(total * fraction)
                if job["status"] == "done":
                    if self.fail_jobs:
                        job["status"], job["error"] = "failed", "mock failure"
                    else:
                        job["result"] = self._transcript(job["seconds"])
            return dict(job)

    def _transcript(self, seconds):
        segments, n, start = [], 0, 0.0
        while start < seconds - 0.5:
            end = min(seconds, start + self.segment_seconds - 1)
            n += 1
            segments.append({"start": round(start, 2), "end": round(end, 2),
                             "text": f"これはテスト発言その{n}です。", "lang": "ja"})
            start += self.segment_seconds
        return {"language": "ja", "duration": seconds, "segments": segments}

    # -- captions WS (minimal RFC 6455 server, unfragmented frames) -----------
    def _captions(self, handler):
        key = handler.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.wfile.flush()
        session = {"start": None, "samples": 0, "binary_frames": 0, "stopped": False, "sent": []}
        self.caption_sessions.append(session)
        rfile, wfile = handler.rfile, handler.wfile

        def send(opcode, payload):
            header = bytes([0x80 | opcode])
            n = len(payload)
            header += bytes([n]) if n < 126 else (bytes([126]) + struct.pack(">H", n) if n < 65536
                                                   else bytes([127]) + struct.pack(">Q", n))
            wfile.write(header + payload)
            wfile.flush()

        def send_json(data):
            session["sent"].append(data)
            send(1, json.dumps(data, ensure_ascii=False).encode())

        seq, speaking, silence, speech_start, last_emit = 0, False, 0, 0.0, 0.0
        try:
            while True:
                head = rfile.read(2)
                if len(head) < 2:
                    return
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", rfile.read(8))[0]
                mask = rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
                data = bytearray(rfile.read(length))
                for i in range(len(data)):
                    data[i] ^= mask[i % 4]
                if opcode == 8:
                    send(8, bytes(data[:2]))
                    return
                if opcode == 9:
                    send(10, bytes(data))
                    continue
                if opcode == 1:
                    msg = json.loads(data.decode())
                    if msg.get("type") == "start":
                        session["start"] = msg
                        send_json({"type": "ready"})
                    elif msg.get("type") == "stop":
                        session["stopped"] = True
                        if speaking:
                            seq += 1
                            t = session["samples"] / 16000
                            send_json({"type": "caption", "seq": seq, "t0": speech_start, "t1": t,
                                       "text": f"最後の字幕 {seq}", "lang": "ja", "final": True})
                        send(8, struct.pack(">H", 1000))
                        return
                    continue
                if opcode != 2 or session["start"] is None:
                    continue
                pcm = memoryview(bytes(data)).cast("h")
                session["binary_frames"] += 1
                t_before = session["samples"] / 16000
                session["samples"] += len(pcm)
                t = session["samples"] / 16000
                if self.caption_every:
                    if t - last_emit >= self.caption_every:
                        seq += 1
                        send_json({"type": "caption", "seq": seq, "t0": round(last_emit, 3), "t1": round(t, 3),
                                   "text": f"テスト字幕 {seq}：会議の音声を認識しています。", "lang": "ja", "final": True})
                        last_emit = t
                    continue
                rms = math.sqrt(sum(s * s for s in pcm) / max(1, len(pcm)))
                if rms > 300:
                    if not speaking:
                        speaking, speech_start = True, t_before
                        send_json({"type": "speech", "active": True, "t": round(t_before, 3)})
                    silence = 0
                elif speaking:
                    silence += len(pcm)
                    if silence >= 8000:
                        seq += 1
                        speaking = False
                        send_json({"type": "speech", "active": False, "t": round(t, 3)})
                        send_json({"type": "caption", "seq": seq, "t0": round(speech_start, 3),
                                   "t1": round(t - silence / 16000, 3), "text": f"発話 {seq}", "lang": "ja",
                                   "final": True})
        except (ConnectionError, OSError):
            return
        finally:
            handler.close_connection = True

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="mock-ai-hub")
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--key", default=None)
    parser.add_argument("--caption-every", type=float, default=None,
                        help="emit a caption every N seconds of audio instead of using VAD")
    parser.add_argument("--job-seconds", type=float, default=5.0)
    args = parser.parse_args()
    hub = MockHub(args.key, args.caption_every, args.job_seconds, host=args.host, port=args.port)
    print(f"mock kvm-ai-hub on {hub.url} (key {'set' if args.key else 'not required'})", flush=True)
    hub.server.serve_forever()


if __name__ == "__main__":
    main()
