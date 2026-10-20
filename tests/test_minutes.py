"""Meeting minutes: schema, summarizer chain, mock CLIs/LLM, resumable jobs."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time

import pytest
import websockets

from serial_hid_kvm import _minutes
from serial_hid_kvm._minutes import (HubClient, MinutesError, MinutesService, markdown_minutes, minutes_page,
                                     parse_json_text, summarize_agy, summarize_codex, summarize_mac_qwen,
                                     summarizer_chain, validate_minutes, validate_segments)
from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm.config import Config
from tests.mock_ai_hub import MockHub
from tests.test_web_auth import _make_server

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
SESSION = "b" * 32


def _good(at=5):
    return {"title": "定例会議", "summary": "進捗を確認した。",
            "topics": [{"title": "進捗", "points": ["API は完成"], "start_sec": at}],
            "decisions": [{"text": "金曜にリリース", "at_sec": at}],
            "todos": [{"task": "手順書を書く", "owner": None, "due": "", "at_sec": at},
                      {"task": "レビュー", "owner": "田中", "due": "10/20", "at_sec": 1}],
            "open_questions": ["本番の時期"], "uncertainties": []}


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def test_validate_minutes_normalizes_and_rejects():
    data = validate_minutes({**_good(), "extra": "dropped"}, duration=60)
    assert "extra" not in data
    assert data["todos"][0]["owner"] is None and data["todos"][0]["due"] is None
    assert data["todos"][1] == {"task": "レビュー", "owner": "田中", "due": "10/20", "at_sec": 1.0}
    with pytest.raises(ValueError, match="超出"):
        validate_minutes(_good(at=500), duration=60)
    for broken in ({k: v for k, v in _good().items() if k != "todos"},
                   {**_good(), "topics": [{"title": "x", "points": "not a list", "start_sec": 1}]},
                   {**_good(), "decisions": [{"text": "x", "at_sec": True}]},
                   {**_good(), "summary": 3}, ["not", "an", "object"]):
        with pytest.raises(ValueError):
            validate_minutes(broken, duration=60)


def test_summarizer_chain_and_privacy():
    order = ["agy", "codex", "mac-qwen"]
    assert summarizer_chain("agy", order) == ["agy", "codex", "mac-qwen"]
    assert summarizer_chain("codex", order) == ["codex", "mac-qwen"]
    assert summarizer_chain("mac-qwen", order) == ["mac-qwen"]
    assert summarizer_chain("mac-qwen", ["mac-qwen", "agy", "codex"]) == ["mac-qwen"]  # never leaves the LAN
    assert summarizer_chain("agy", ["codex", "agy", "mac-qwen"]) == ["agy", "mac-qwen"]
    assert summarizer_chain("off", order) == []


def test_parse_json_text_strips_thinking_and_fences():
    assert parse_json_text('<think>hmm {"no":1}</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(MinutesError):
        parse_json_text("no json here")


def test_segments_and_markdown_render():
    segs = validate_segments({"segments": [{"start": 70.5, "end": 75, "text": " 二 ", "lang": "ja"},
                                           {"start": 1, "end": 3, "text": "一"}, {"start": 4, "end": 5, "text": " "}]})
    assert [s["text"] for s in segs] == ["一", "二"]
    with pytest.raises(MinutesError):
        validate_segments({"segments": [{"start": "x", "end": 1, "text": "a"}]})
    md = markdown_minutes(validate_minutes(_good(), 60), "ja", "rec", "rec file.mp4", 60, "agy", "rec.srt")
    assert "## 決定事項" in md and "[00:00:05](rec%20file.mp4#t=5)" in md
    assert "| 手順書を書く | 未定 | 未定 |" in md
    zh = markdown_minutes(validate_minutes(_good(), 60), "zh", "rec", "rec.mp4", 60, "codex", "rec.srt")
    assert "## 决定事项" in zh


def test_minutes_page_escapes_model_text():
    data = validate_minutes({**_good(), "title": "<script>alert(1)</script>",
                             "summary": '"><img src=x onerror=alert(1)>'}, 60)
    page = minutes_page(data, "ja", data["title"], "/recordings/file?a=1&b=2", [], "/m?x", None, nonce="n0")
    assert "<script>alert" not in page and "<img" not in page
    assert '<script nonce="n0">' in page and 'src="/recordings/file?a=1&amp;b=2"' in page


# ---------------------------------------------------------------------------
# Summarizer backends (fake CLIs / fake OpenAI server)
# ---------------------------------------------------------------------------

def _wrapper(tmp_path, name, source):
    script = tmp_path / f"fake_{name}.py"
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


FAKE_CODEX = r'''
import json, os, sys
args = sys.argv[1:]
val = lambda flag: args[args.index(flag) + 1]
assert args[0] == "exec" and args[-1] == "-"
assert val("--sandbox") == "read-only"
assert "--ignore-user-config" in args and "--ephemeral" in args
assert "shell_tool" in args and "browser_use" in args
root = val("-C")
assert os.listdir(root) == [] and os.path.samefile(os.getcwd(), root)
schema = json.load(open(val("--output-schema"), encoding="utf-8"))
assert schema["properties"]["todos"]["items"]["properties"]["owner"]["type"] == ["string", "null"]
prompt = sys.stdin.buffer.read().decode("utf-8")
assert "untrusted DATA" in prompt and "<transcript>" in prompt
open(os.environ["FAKE_LOG"], "w", encoding="utf-8").write(prompt)
open(val("-o"), "w", encoding="utf-8").write(os.environ["FAKE_RESULT"])
'''

FAKE_AGY = r'''
import json, os, re, shlex, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
assert "--sandbox" in args and args[args.index("--mode") + 1] == "plan"
assert json.load(open(args[args.index("--json-schema") + 1]))["required"][0] == "title"
hooks = json.load(open(Path(".agents") / "hooks.json"))["sop-read-only"]
prompt = json.loads(sys.stdin.readline())["message"]["content"]
target = re.search(r"read exactly this one file: (.+?) \(the meeting transcript\)", prompt).group(1)
def hook(command, payload):
    command = command if os.name == "nt" else shlex.split(command)
    out = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True).stdout
    return json.loads(out)
hook(hooks["SessionStart"][0]["command"], {})
pre = hooks["PreToolUse"][0]["hooks"][0]["command"]
post = hooks["PostToolUse"][0]["hooks"][0]["command"]
denied = [hook(pre, {"toolCall": {"name": "view_file", "args": {"AbsolutePath": str(Path("other.txt").resolve())}}}),
          hook(pre, {"toolCall": {"name": "run_command", "args": {"CommandLine": "calc"}}})]
assert all(d["decision"] == "deny" for d in denied), denied
call = {"toolCall": {"name": "view_file", "args": {"AbsolutePath": target}}, "modelName": "fake-model"}
assert hook(pre, call)["decision"] == "auto_approve"
text = open(target, encoding="utf-8").read()
assert "[00:00:00]" in text
hook(post, call)
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": json.loads(os.environ["FAKE_RESULT"])}}))
'''


def test_codex_backend_uses_read_only_sandbox_and_schema(tmp_path, monkeypatch):
    exe = _wrapper(tmp_path, "codex", FAKE_CODEX)
    monkeypatch.setenv("FAKE_RESULT", json.dumps(_good()))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "prompt.txt"))
    data = summarize_codex(str(exe), tmp_path / "work", "untrusted DATA prompt", "[00:00:00] こんにちは", 60, lambda: None)
    assert data["title"] == "定例会議"
    assert "[00:00:00] こんにちは" in (tmp_path / "prompt.txt").read_text(encoding="utf-8")


def test_codex_backend_failure_is_reported(tmp_path, monkeypatch):
    exe = _wrapper(tmp_path, "codex", "import sys; sys.exit(3)\n")
    with pytest.raises(MinutesError, match="codex"):
        summarize_codex(str(exe), tmp_path / "work", "p", "t", 60, lambda: None)


def test_agy_backend_requires_guarded_transcript_read(tmp_path, monkeypatch):
    exe = _wrapper(tmp_path, "agy", FAKE_AGY)
    monkeypatch.setenv("FAKE_RESULT", json.dumps(_good()))
    work = tmp_path / "agy"
    work.mkdir()
    transcript = work / "transcript.txt"
    transcript.write_text("[00:00:00] 開始します\n", encoding="utf-8")
    data = summarize_agy(str(exe), work, transcript, "untrusted DATA", 60, lambda: None)
    assert data["decisions"][0]["text"] == "金曜にリリース"
    audit = [json.loads(line) for line in (work / ".agents" / "audit.jsonl").read_text().splitlines()]
    assert [a["decision"] for a in audit if a["mode"] == "pre"] == ["deny", "deny", "auto_approve"]
    mcp = json.loads((work / ".agents" / "mcp_config.json").read_text())
    assert all(server.get("disabled") for server in mcp["mcpServers"].values())


def test_agy_backend_accepts_read_file_from_newer_models(tmp_path, monkeypatch):
    source = FAKE_AGY.replace('{"name": "view_file", "args": {"AbsolutePath": target}}',
                              '{"name": "read_file", "args": {"Path": target}}')
    exe = _wrapper(tmp_path, "agy", source)
    monkeypatch.setenv("FAKE_RESULT", json.dumps(_good()))
    work = tmp_path / "agy"
    work.mkdir()
    (work / "transcript.txt").write_text("[00:00:00] x\n", encoding="utf-8")
    data = summarize_agy(str(exe), work, work / "transcript.txt", "p", 60, lambda: None)
    assert data["title"] == "定例会議"
    audit = [json.loads(line) for line in (work / ".agents" / "audit.jsonl").read_text().splitlines()]
    assert [(a["tool"], a["decision"]) for a in audit if a["mode"] == "post"] == [("read_file", "auto_approve")]


def test_agy_backend_without_read_evidence_is_rejected(tmp_path, monkeypatch):
    source = FAKE_AGY.replace('hook(post, call)\n', '')
    exe = _wrapper(tmp_path, "agy", source)
    monkeypatch.setenv("FAKE_RESULT", json.dumps(_good()))
    work = tmp_path / "agy"
    work.mkdir()
    (work / "transcript.txt").write_text("[00:00:00] x\n", encoding="utf-8")
    with pytest.raises(MinutesError, match="证据"):
        summarize_agy(str(exe), work, work / "transcript.txt", "p", 60, lambda: None)


class _FakeLLM:
    def __init__(self):
        self.requests = []
        llm = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                llm.requests.append((self.path, self.headers.get("Authorization"), body))
                if "response_format" in body:
                    self.send_response(400)
                    self.end_headers()
                    return
                content = "<think>考え中</think>```json\n" + json.dumps(_good(), ensure_ascii=False) + "\n```"
                reply = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


def test_mac_qwen_backend_retries_without_optional_switches():
    llm = _FakeLLM()
    try:
        data = summarize_mac_qwen(llm.url, "mac-key", "current", "SYSTEM", "[00:00:01] hi", 30)
    finally:
        llm.server.shutdown()
    assert data["title"] == "定例会議"
    (path, auth, first), (_, _, second) = llm.requests
    assert path == "/v1/chat/completions" and auth == "Bearer mac-key"
    assert first["chat_template_kwargs"] == {"enable_thinking": False} and "response_format" not in second
    assert first["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert "<transcript>" in first["messages"][1]["content"] and first["model"] == "current"


# ---------------------------------------------------------------------------
# Hub client + resumable job pipeline
# ---------------------------------------------------------------------------

def _make_webm(path, seconds=25, audio=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=64x36:rate=5:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-c:a", "libopus"]
    subprocess.run(args + ["-c:v", "libvpx", str(path)], check=True, capture_output=True)
    return path


def _service(root, hub, **config):
    cfg = Config()
    cfg.recording_dir = str(root)
    cfg.ai_hub_url = hub.url if hub else cfg.ai_hub_url
    cfg.ai_hub_key = hub.key if hub else None
    for key, value in config.items():
        setattr(cfg, key, value)
    service = MinutesService(cfg, RecordingLibrary(root))
    service.POLL = 0.05
    return service


def _record(root, audio=True):
    source = _make_webm(root / ".recording-sessions" / SESSION / "source.webm", audio=audio)
    stem = "recording-2026-10-18T10-00-00-000-JST-bbbbbbbb"
    return source, stem, RecordingLibrary(root).identifier(stem + ".mp4")


def _runner(result=None, error=None, calls=None):
    def run(job, folder, prompt, transcript, transcript_path, duration):
        if calls is not None:
            calls.append((folder.name, prompt, transcript))
        if error:
            raise MinutesError(error)
        return result
    return run


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg required")
def test_pipeline_produces_transcript_srt_and_minutes(tmp_path):
    hub = MockHub(key="hub-key", job_seconds=0.3).start()
    try:
        service = _service(tmp_path, hub)
        calls = []
        service.runners = {"agy": _runner(error="agy broke", calls=calls),
                           "codex": _runner(_good(), calls=calls), "mac-qwen": _runner(error="unused")}
        source, stem, key = _record(tmp_path)
        job = service.enqueue(key, source, stem, "agy", SESSION)
        assert service.enqueue(key, source, stem, "codex")["summarizer"] == "agy"  # idempotent
        service.process(job)
        job = service.load(key)
        assert job["status"] == "done" and job["used"] == "codex"
        assert [a["summarizer"] for a in job["attempts"]] == ["agy"]
        assert [c[0] for c in calls] == ["summary-agy", "summary-codex"]
        assert "untrusted DATA" in calls[1][1] and "[00:00:10] これはテスト発言その2です。" in calls[1][2]
        assert hub.uploads[0]["language"] == "auto" and abs(hub.uploads[0]["seconds"] - 25) < 0.5
        assert (tmp_path / (stem + ".srt")).read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> ")
        md = (tmp_path / (stem + ".minutes.md")).read_text(encoding="utf-8")
        assert "# 定例会議" in md and f"({stem}.mp4#t=5)" in md
        assert not hub.jobs  # finished hub job is deleted
        view = service.view(key)
        assert view["state"] == "done" and view["label"] == "已完成 · codex"
        assert set(view["links"]) == {"minutes", "minutes_md", "transcript"}
        md_path = service.library.resolve_link(_query(view["links"]["minutes"]), suffixes=(".md",))
        page = service.page(md_path, "nonce")
        assert "定例会議" in page and ".srt" in page and "これはテスト発言その1です。" in page
    finally:
        hub.stop()


def _query(url):
    from urllib.parse import parse_qs, urlsplit
    return parse_qs(urlsplit(url).query)


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg required")
def test_failed_summary_retry_reuses_transcript_and_restart_resumes_hub_job(tmp_path):
    hub = MockHub(job_seconds=0.3).start()
    try:
        service = _service(tmp_path, hub)
        service.runners = {name: _runner(error=f"{name} down") for name in ("agy", "codex", "mac-qwen")}
        source, stem, key = _record(tmp_path)
        service.enqueue(key, source, stem, "codex")
        threading.Thread(target=service.run, daemon=True).start()
        for _ in range(200):
            if (service.load(key) or {}).get("status") == "failed":
                break
            time.sleep(0.05)
        service.stop()
        job = service.load(key)
        assert job["status"] == "failed" and "codex: codex down" in job["error"] and "mac-qwen" in job["error"]
        assert "agy" not in job["chain"]
        assert service.view(key)["state"] == "failed"
        assert not (tmp_path / ".minutes" / key / "lock").exists()

        # Retry after a "restart": transcript is reused, nothing is uploaded again.
        restarted = _service(tmp_path, hub)
        restarted.runners = {"codex": _runner(_good()), "mac-qwen": _runner(error="x"), "agy": _runner(error="x")}
        restarted.retry(key)
        restarted.process(restarted.load(key))
        assert restarted.load(key)["status"] == "done" and len(hub.uploads) == 1
        # Re-summarize a done job (same summarizer): new summary, same transcript.
        restarted.runners["codex"] = _runner({**_good(), "title": "改訂版"})
        restarted.retry(key, "codex")
        restarted.process(restarted.load(key))
        assert "# 改訂版" in (tmp_path / (stem + ".minutes.md")).read_text(encoding="utf-8")
        assert len(hub.uploads) == 1

        # A job interrupted while the hub was transcribing resumes polling the same hub job.
        source2 = _make_webm(tmp_path / ".recording-sessions" / ("c" * 32) / "source.webm", seconds=12)
        key2 = restarted.library.identifier("second.mp4")
        job2 = restarted.enqueue(key2, source2, "second", "mac-qwen")
        flac = restarted._dir(key2) / "audio.flac"
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(source2), "-ac", "1", "-ar", "16000",
                        str(flac)], check=True)
        hub_job = HubClient(hub.url).submit(flac)
        restarted._save(job2, status="transcribing", hub_job=hub_job)
        resumed = _service(tmp_path, hub)
        resumed.runners = {"mac-qwen": _runner(_good(at=3))}
        assert [j["id"] for j in resumed.pending()] == [key2]
        resumed.process(resumed.load(key2))
        assert resumed.load(key2)["status"] == "done" and len(hub.uploads) == 2
    finally:
        hub.stop()


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg required")
def test_hub_failure_no_audio_and_silent_meeting(tmp_path):
    hub = MockHub(job_seconds=0.1, fail_jobs=True).start()
    try:
        service = _service(tmp_path, hub)
        source, stem, key = _record(tmp_path)
        job = service.enqueue(key, source, stem, "agy")
        with pytest.raises(MinutesError, match="hub 转写失败"):
            service.process(job)
        # Recording without audio track
        silent = _make_webm(tmp_path / ".recording-sessions" / ("d" * 32) / "source.webm", seconds=3, audio=False)
        key2 = service.library.identifier("noaudio.mp4")
        with pytest.raises(MinutesError, match="音轨"):
            service.process(service.enqueue(key2, silent, "noaudio", "agy"))
    finally:
        hub.stop()
    # Hub found no speech: minutes are written without calling any model.
    hub = MockHub(job_seconds=0.1, segment_seconds=1000).start()
    try:
        service = _service(tmp_path, hub)
        service.runners = {}
        hub._transcript = lambda seconds: {"language": "ja", "duration": seconds, "segments": []}
        source3 = _make_webm(tmp_path / ".recording-sessions" / ("e" * 32) / "source.webm", seconds=3)
        key3 = service.library.identifier("quiet.mp4")
        service.process(service.enqueue(key3, source3, "quiet", "agy"))
        assert service.load(key3)["used"] == "none"
        assert "発言を検出できませんでした" in (tmp_path / "quiet.minutes.md").read_text(encoding="utf-8")
    finally:
        hub.stop()


def test_hub_auth_error_mentions_env_var_not_key(tmp_path):
    hub = MockHub(key="right").start()
    try:
        flac = tmp_path / "a.flac"
        flac.write_bytes(b"fLaC" + bytes(40))
        with pytest.raises(MinutesError) as error:
            HubClient(hub.url, "wrong-key").submit(flac)
        assert "SHKVM_AI_HUB_KEY" in str(error.value) and "wrong-key" not in str(error.value)
        assert HubClient(hub.url).health()["status"] == "ok"
    finally:
        hub.stop()


def test_off_choice_and_lock_between_processes(tmp_path):
    service = _service(tmp_path, None)
    source = tmp_path / ".recording-sessions" / SESSION / "source.webm"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    assert service.enqueue("f" * 32, source, "x", "off") is None
    service.enqueue("f" * 32, source, "x", "mac-qwen")
    other = _service(tmp_path, None)
    assert service._acquire("f" * 32) and not other._acquire("f" * 32)
    service._release("f" * 32)
    assert other._acquire("f" * 32)
    disabled = _service(tmp_path, None, minutes_enabled=False)
    assert disabled.enqueue("a" * 32, source, "y", "agy") is None


# ---------------------------------------------------------------------------
# Viewer: choice is stored at rec_session_start and queued at finish
# ---------------------------------------------------------------------------

def test_viewer_stores_choice_and_queues_minutes(tmp_path):
    async def run():
        server, url = _make_server(recording_dir=str(tmp_path))
        queued = []
        server._minutes.enqueue = lambda *args: queued.append(args) or {"id": args[0]}
        server._export_recording = lambda *a, **k: None

        async def receive(ws, kind):
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if msg["type"] == kind:
                    return msg
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "rec_session_start", "session": SESSION,
                                          "filename": "recording-2026-10-18T10-00-00.webm", "minutes": "mac-qwen"}))
                await receive(ws, "rec_session_ready")
                manifest = json.loads((tmp_path / ".recording-sessions" / SESSION / "session.json").read_text())
                assert manifest["minutes"] == "mac-qwen" and manifest["started_epoch"] > 0
                await ws.send(b"\x11" + struct.pack(">I", 0) + b"data")
                await receive(ws, "rec_ack")
                await ws.send(json.dumps({"type": "rec_session_finish", "session": SESSION, "next": 1}))
                saved = await receive(ws, "rec_session_saved")
                await ws.send(json.dumps({"type": "minutes_retry", "id": "0" * 32}))
                assert "error" in await receive(ws, "minutes_action")
                await ws.send(json.dumps({"type": "recordings_list"}))
                listing = await receive(ws, "recordings_list")
        finally:
            await server.stop()
        (key, source, stem, choice, session), = queued
        assert choice == "mac-qwen" and session == SESSION and key == saved["recording_id"]
        assert stem == "recording-2026-10-18T10-00-00-bbbbbbbb" and source.name == "source.webm"
        assert listing["minutes_enabled"] is True and listing["items"][0]["minutes"] is None
    asyncio.run(run())


def test_text_side_files_are_served_with_signed_links(tmp_path):
    (tmp_path / "rec.minutes.md").write_text("# 会議\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    library = RecordingLibrary(tmp_path)
    path = library.resolve_link(_query(library.file_url("rec.minutes.md")))
    assert path.name == "rec.minutes.md"
    with pytest.raises(FileNotFoundError):
        library.resolve_link(_query(library.file_url("secret.txt")))
    with pytest.raises(ValueError):
        library.resolve_link({"file": ["rec.minutes.md"], "expires": ["99999999999"], "key": ["forged"]})
