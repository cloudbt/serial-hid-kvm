"""Server-side recording: capture frames + HDMI / Host-mic audio -> H.264/AAC MKV.

The browser only sends start/stop, so no video is buffered in a tab (Chrome
tabs ran out of memory with MediaRecorder at 1080p). One recording per Target
process; it keeps running when every viewer disconnects.

Encoding runs in an ffmpeg subprocess (PyAV encoded 1080p at only ~15 fps on
this Host; the ffmpeg CLI does ~56 fps with libx264 veryfast). Raw BGR frames go
to its stdin at a constant ``fps`` from the wall clock, repeating the latest
capture frame when nothing changed (repeats cost x264 almost nothing). Audio is
mixed here in 20 ms blocks, also from the wall clock, and streamed to ffmpeg over
a 127.0.0.1 socket. A short input is padded with silence and a backlog over
300 ms is dropped, which absorbs clock drift between the two sound cards.

The output is Matroska so a crash still leaves a readable file; the normal
export remuxes it to MP4 without re-encoding.
"""
from __future__ import annotations

import collections
import logging
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK = 960                      # 20 ms at 48 kHz
MAX_BACKLOG_BLOCKS = 15          # 300 ms
FPS_CHOICES = (15, 30)
SOURCES = ("mic", "hdmi", "mix", "none")
ENCODERS = ("h264_amf", "libx264")
DISK_CHECK_SECONDS = 10.0


def find_input_device(name: str):
    """Index of the first input device whose name contains *name*.

    WASAPI is preferred (stable full names); other host APIs are a fallback.
    Returns None when nothing matches.
    """
    import sounddevice as sd
    apis = sd.query_hostapis()
    devices = list(enumerate(sd.query_devices()))
    wanted = name.casefold()
    for preferred in ("Windows WASAPI", None):
        matches = [i for i, d in devices
                   if d["max_input_channels"] > 0 and wanted in d["name"].casefold()
                   and (preferred is None or apis[d["hostapi"]]["name"] == preferred)]
        if matches:
            return matches[0]
    return None


def to_block_format(pcm: np.ndarray, rate: int, channels: int) -> np.ndarray:
    """int16 interleaved PCM at any rate/channels -> int16 (n, 2) at 48 kHz."""
    data = np.asarray(pcm, dtype=np.int16).reshape(-1, channels)
    if channels == 1:
        data = np.repeat(data, 2, axis=1)
    elif channels > 2:
        data = data[:, :2]
    if rate != SAMPLE_RATE and len(data):
        n = max(1, int(round(len(data) * SAMPLE_RATE / rate)))
        src = np.arange(len(data))
        dst = np.linspace(0, len(data) - 1, n)
        data = np.stack([np.interp(dst, src, data[:, c]) for c in range(2)], axis=1)
    return np.asarray(data, dtype=np.int16)


class AudioBuffer:
    """Thread-safe FIFO of 48 kHz stereo samples, read in fixed blocks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._chunks: collections.deque = collections.deque()
        self._size = 0

    def push(self, samples: np.ndarray):
        if not len(samples):
            return
        with self._lock:
            self._chunks.append(samples)
            self._size += len(samples)
            excess = self._size - MAX_BACKLOG_BLOCKS * BLOCK
            while excess > 0 and self._chunks:   # drop the oldest backlog
                head = self._chunks[0]
                if len(head) <= excess:
                    self._chunks.popleft()
                    self._size -= len(head)
                    excess -= len(head)
                else:
                    self._chunks[0] = head[excess:]
                    self._size -= excess
                    excess = 0

    def read(self, frames: int = BLOCK) -> np.ndarray:
        out = np.zeros((frames, CHANNELS), dtype=np.int16)
        filled = 0
        with self._lock:
            while filled < frames and self._chunks:
                head = self._chunks[0]
                take = min(frames - filled, len(head))
                out[filled:filled + take] = head[:take]
                filled += take
                if take == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[take:]
                self._size -= take
        return out


def mix(blocks) -> np.ndarray:
    total = np.zeros((BLOCK, CHANNELS), dtype=np.int32)
    for block in blocks:
        total += block
    return np.clip(total, -32768, 32767).astype(np.int16)


class HdmiFeed:
    """Moves PCM from the shared server AudioCapture into an AudioBuffer."""

    def __init__(self, capture, buffer: AudioBuffer):
        self._capture, self._buffer = capture, buffer
        self._queue = capture.subscribe()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="srec-hdmi", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._buffer.push(to_block_format(np.frombuffer(chunk, dtype=np.int16),
                                              self._capture.samplerate, self._capture.channels))

    def close(self):
        self._stop.set()
        self._capture.unsubscribe(self._queue)
        self._thread.join(timeout=1)


class MicFeed:
    """Host microphone (e.g. EarPods) captured by the server itself."""

    def __init__(self, device: int, buffer: AudioBuffer):
        import sounddevice as sd
        info = sd.query_devices(device, kind="input")
        self.name = info["name"]
        self._rate = int(info["default_samplerate"])
        self._channels = min(2, int(info["max_input_channels"]))
        self._buffer = buffer
        self._stream = sd.InputStream(device=device, samplerate=self._rate, channels=self._channels,
                                      dtype="int16", blocksize=int(self._rate / 50), callback=self._callback)
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        self._buffer.push(to_block_format(indata.copy(), self._rate, self._channels))

    def close(self):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class FFmpegWriter:
    """ffmpeg process: rawvideo on stdin + s16le audio on a local socket -> MKV."""

    def __init__(self, path: Path, width: int, height: int, fps: int, audio: bool, encoder: str):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found")
        self.width, self.height = width, height
        self.log = path.with_suffix(".ffmpeg.log")
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        port = None
        if audio:
            # Audio is input 0: ffmpeg opens inputs in order and the rawvideo
            # stdin input blocks on its first read, so it must come second.
            with socket.socket() as probe:          # pick a free port for ffmpeg to listen on
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            args += ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                     "-thread_queue_size", "512", "-i", f"tcp://127.0.0.1:{port}?listen=1"]
        args += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
                 "-framerate", str(fps), "-thread_queue_size", "64", "-i", "pipe:0"]
        args += (["-map", "1:v", "-map", "0:a"] if audio else ["-map", "0:v"])
        if encoder == "h264_amf":
            args += ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "speed",
                     "-b:v", "4M" if fps <= 15 else "6M"]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        args += ["-pix_fmt", "yuv420p", "-g", str(fps * 2)]
        if audio:
            args += ["-c:a", "aac", "-b:a", "128k"]
        args += ["-f", "matroska", str(path)]
        self._log_file = self.log.open("wb")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self._log_file,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.audio_sock = None
        if audio:
            deadline = time.monotonic() + 10
            while self.audio_sock is None:
                if self.proc.poll() is not None:
                    raise RuntimeError(self.error() or "ffmpeg exited")
                try:
                    self.audio_sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                except OSError:
                    if time.monotonic() > deadline:
                        self.close(timeout=5)
                        raise RuntimeError("ffmpeg audio input did not open")
                    time.sleep(0.05)

    def error(self) -> str:
        try:
            self._log_file.flush()
            return self.log.read_text(encoding="utf-8", errors="replace").strip()[-500:]
        except (OSError, ValueError):
            return ""

    def write_video(self, data: bytes):
        self.proc.stdin.write(data)

    def write_audio(self, data: bytes):
        self.audio_sock.sendall(data)

    def close(self, timeout: float = 60) -> int:
        for closer in (lambda: self.proc.stdin.close(),
                       lambda: self.audio_sock and self.audio_sock.close()):
            try:
                closer()
            except OSError:
                pass
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        self._log_file.close()
        return code


class ServerRecorder:
    """One server-side recording at a time for this Target process."""

    def __init__(self, capture, hdmi_audio=None, mic_name: str = "EarPods",
                 max_seconds: float = 3 * 3600, min_free_bytes: int = 2 * 1024 ** 3,
                 encoders=ENCODERS, on_stop=None):
        # A ScreenCapture, or a zero-argument function returning one (resolved at
        # start so constructing the recorder never opens the capture device).
        self._capture_source = capture
        self._capture = None
        self._hdmi_audio = hdmi_audio
        self._mic_name = mic_name
        self.max_seconds = max_seconds
        self.min_free_bytes = min_free_bytes
        self._encoders = tuple(encoders)
        self.on_stop = on_stop          # called with the final status from the worker thread
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict = {"active": False}

    # ---- public API -------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            state = dict(self._status)
            state["warnings"] = list(state.get("warnings", []))
        if state.get("active") and state.get("started_monotonic") is not None:
            state["elapsed"] = time.monotonic() - state["started_monotonic"]
        state.pop("started_monotonic", None)
        return state

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._status.get("active"))

    def start(self, session: str, path: Path, fps: int = 15, source: str = "mix",
              started_epoch: float | None = None) -> dict:
        if fps not in FPS_CHOICES:
            raise ValueError("fps must be 15 or 30")
        if source not in SOURCES:
            raise ValueError("Invalid audio source")
        with self._lock:
            if self._status.get("active"):
                raise RuntimeError("A server recording is already running")
            self._status = {"active": True, "starting": True, "session": session, "path": str(path),
                            "fps": fps, "source": source, "bytes": 0, "frames": 0,
                            "started_epoch": started_epoch or time.time(),
                            "started_monotonic": time.monotonic(), "warnings": []}
        self._stop.clear()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(Path(path), fps, source, ready),
                                        name="server-recorder", daemon=True)
        self._thread.start()
        ready.wait(timeout=30)          # encoder opened (or failed) and inputs attached
        state = self.status()
        if not state.get("active") and state.get("error"):
            raise RuntimeError(state["error"])
        return state

    def stop(self, reason: str = "manual", timeout: float = 90) -> dict:
        thread = self._thread
        with self._lock:
            if self._status.get("active"):
                self._status.setdefault("stopped_reason", reason)
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return self.status()

    # ---- worker -------------------------------------------------------------
    def _warn(self, message: str):
        logger.warning("Server recording: %s", message)
        with self._lock:
            self._status.setdefault("warnings", []).append(message)

    def _set_reason(self, reason: str):
        with self._lock:
            self._status.setdefault("stopped_reason", reason)

    def _open_writer(self, path, width, height, fps, audio):
        last_error = None
        for encoder in self._encoders:
            writer = None
            try:
                writer = FFmpegWriter(path, width, height, fps, audio, encoder)
                # A missing hardware encoder makes ffmpeg exit on the first frame.
                writer.write_video(bytes(width * height * 3))
                time.sleep(0.5)
                if writer.proc.poll() is not None:
                    raise RuntimeError(writer.error() or "ffmpeg exited")
                return writer, encoder
            except Exception as exc:
                last_error = exc
                logger.info("Encoder %s unavailable: %s", encoder, exc)
                if writer is not None:
                    writer.close(timeout=5)
                path.unlink(missing_ok=True)
        raise RuntimeError(f"No usable H.264 encoder: {last_error}")

    def _attach_audio(self, source):
        feeds, buffers = [], []
        if source in ("hdmi", "mix"):
            if self._hdmi_audio is not None:
                buf = AudioBuffer()
                feeds.append(HdmiFeed(self._hdmi_audio, buf))
                buffers.append(buf)
            else:
                self._warn("Target HDMI audio is not available on this server")
        if source in ("mic", "mix"):
            device = find_input_device(self._mic_name) if self._mic_name else None
            if device is None:
                self._warn(f"Host microphone '{self._mic_name}' not found")
            else:
                buf = AudioBuffer()
                try:
                    feeds.append(MicFeed(device, buf))
                    buffers.append(buf)
                except Exception as exc:
                    self._warn(f"Host microphone failed: {exc}")
        return feeds, buffers

    def _run(self, path: Path, fps: int, source: str, ready: threading.Event):
        feeds, writer, threads = [], None, []
        failure = threading.Event()
        try:
            source_obj = self._capture_source
            self._capture = source_obj() if callable(source_obj) and not hasattr(source_obj, "ensure_streaming") else source_obj
            self._capture.ensure_streaming()
            frame, seq = None, -1
            deadline = time.monotonic() + 15
            while frame is None and time.monotonic() < deadline and not self._stop.is_set():
                frame, seq = self._capture.get_frame_if_newer(-1)
                if frame is None:
                    time.sleep(0.1)
            if frame is None:
                raise RuntimeError("No video from the capture device")
            height, width = frame.shape[0] & ~1, frame.shape[1] & ~1
            path.parent.mkdir(parents=True, exist_ok=True)
            writer, encoder = self._open_writer(path, width, height, fps, source != "none")
            feeds, buffers = self._attach_audio(source) if source != "none" else ([], [])
            t0 = time.monotonic()
            with self._lock:
                self._status.update(starting=False, encoder=encoder, width=width, height=height,
                                    audio_inputs=len(buffers), started_monotonic=t0)
            logger.info("Server recording started: %s (%s, %dx%d @ %d fps, audio %s, %d input(s))",
                        path.name, encoder, width, height, fps, source, len(buffers))
            ready.set()
            if source != "none":
                threads.append(threading.Thread(target=self._audio_loop, name="srec-audio", daemon=True,
                                                args=(writer, buffers, t0, failure)))
                threads[-1].start()
            self._video_loop(writer, fps, frame, seq, t0, path, failure)
        except Exception as exc:
            logger.exception("Server recording failed")
            with self._lock:
                self._status["error"] = str(exc)
                self._status.setdefault("stopped_reason", "error")
        finally:
            self._stop.set()
            for thread in threads:
                thread.join(timeout=5)
            for feed in feeds:
                feed.close()
            if writer is not None:
                code = writer.close()
                if code:
                    self._warn(f"ffmpeg exited with {code}: {writer.error()}")
            with self._lock:
                self._status["active"] = False
                self._status["starting"] = False
                if path.exists():
                    self._status["bytes"] = path.stat().st_size
                self._status["duration"] = time.monotonic() - self._status.get("started_monotonic", time.monotonic())
            ready.set()
            final = self.status()
            logger.info("Server recording stopped (%s): %s, %.1f s, %d frames",
                        final.get("stopped_reason", "manual"), path.name, final.get("duration", 0),
                        final.get("frames", 0))
            if self.on_stop:
                try:
                    self.on_stop(final)
                except Exception:
                    logger.exception("Server recording stop callback failed")

    def _video_loop(self, writer, fps, frame, seq, t0, path, failure):
        data = self._fit(frame, writer.width, writer.height)
        written = 1                       # slot 0 is the encoder probe frame from _open_writer
        next_disk = t0 + DISK_CHECK_SECONDS
        while not self._stop.is_set() and not failure.is_set():
            now = time.monotonic()
            if now - t0 >= self.max_seconds:
                self._set_reason("max_duration")
                break
            if now >= next_disk:
                next_disk = now + DISK_CHECK_SECONDS
                if shutil.disk_usage(path.parent).free < self.min_free_bytes:
                    self._set_reason("disk_full")
                    break
                self._capture.ensure_streaming()
                with self._lock:
                    self._status["bytes"] = path.stat().st_size if path.exists() else 0
            due = int((now - t0) * fps) + 1          # frames that should exist by now
            if due > written:
                new, new_seq = self._capture.get_frame_if_newer(seq)
                if new is not None:
                    seq, data = new_seq, self._fit(new, writer.width, writer.height)
                try:
                    # Constant frame rate: repeat the latest picture for every due slot.
                    while written < due:
                        writer.write_video(data)
                        written += 1
                except OSError as exc:
                    raise RuntimeError(f"ffmpeg stopped accepting video: {writer.error() or exc}")
                with self._lock:
                    self._status["frames"] = written
            self._stop.wait(max(0.0, t0 + written / fps - time.monotonic()))

    def _audio_loop(self, writer, buffers, t0, failure):
        samples = 0
        try:
            while not self._stop.is_set():
                due = (time.monotonic() - t0) * SAMPLE_RATE
                while samples + BLOCK <= due:
                    block = mix([b.read() for b in buffers]) if buffers else np.zeros((BLOCK, CHANNELS), np.int16)
                    writer.write_audio(block.tobytes())
                    samples += BLOCK
                self._stop.wait(0.01)
        except OSError as exc:
            self._warn(f"ffmpeg stopped accepting audio: {exc}")
            failure.set()

    @staticmethod
    def _fit(frame, width, height) -> memoryview:
        """Crop/pad the BGR frame to the fixed output size (autocrop may change it).

        Returns a view of the frame's own memory: capture frames are already
        private copies, so the pipe write needs no further 6 MB copy per frame.
        """
        h, w = frame.shape[:2]
        if (h, w) != (height, width):
            out = np.zeros((height, width, 3), dtype=np.uint8)
            ch, cw = min(h, height), min(w, width)
            out[:ch, :cw] = frame[:ch, :cw]
            frame = out
        return memoryview(np.ascontiguousarray(frame)).cast("B")
