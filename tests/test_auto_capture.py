"""Automatic meeting capture: detector, mic focus, audio sessions, daily .md, viewer protocol."""
import asyncio
from datetime import date, datetime
import json
import queue
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
import pytest
import websockets

from serial_hid_kvm import _auto_capture
from serial_hid_kvm._auto_capture import JST, AutoCapture, Presence, SoundDetector, meetings_root, rms_dbfs
from serial_hid_kvm._captions import CaptionService
from serial_hid_kvm._daily_captions import DailyCaptions
from serial_hid_kvm.config import Config

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeAudio:
    """AudioCapture stand-in: 48 kHz stereo int16, pushed by a pump thread."""
    samplerate, channels = 48000, 2

    def __init__(self):
        self.queues = []
        self.loud = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.queues:
            self.queues.remove(q)

    def _pump(self):
        t = np.arange(960) / 48000
        tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
        loud = np.repeat(tone[:, None], 2, axis=1).tobytes()
        quiet = bytes(960 * 4)
        while not self._stop.is_set():
            for q in list(self.queues):
                try:
                    q.put_nowait(loud if self.loud else quiet)
                except queue.Full:
                    pass
            time.sleep(0.02)

    def close(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Detector / levels / paths
# ---------------------------------------------------------------------------

def test_detector_needs_three_seconds_of_sound_and_stops_after_silence():
    d = SoundDetector(-55, window=10, need=3, silence=180)
    now = 0.0
    for _ in range(25):                     # 2.5 s of sound: not yet
        assert d.feed(-20, 0.1, now) is None
        now += 0.1
    events = [d.feed(-20, 0.1, now + i * 0.1) for i in range(10)]
    assert "start" in events and d.active
    now += 1.0
    assert d.feed(-90, 0.1, now + 179) is None
    assert d.feed(-90, 0.1, now + 181) == "stop" and not d.active


def test_detector_ignores_short_blips_spread_over_time():
    d = SoundDetector(-55, window=10, need=3, silence=180)
    for second in range(60):                # 0.5 s notification sound every 5 s
        for i in range(10):
            loud = second % 5 == 0 and i < 5
            assert d.feed(-20 if loud else -90, 0.1, second + i / 10) is None


def test_rms_dbfs():
    assert rms_dbfs(bytes(1920)) == -120.0
    full = np.full(960, 16384, np.int16).tobytes()
    assert abs(rms_dbfs(full) - (-6.02)) < 0.1


def test_meetings_root_beside_recordings(tmp_path):
    config = Config()
    config.recording_dir = str(tmp_path / "TargetPC1" / "recordings")
    assert meetings_root(config) == tmp_path / "TargetPC1" / "meetings"
    config.meetings_dir = str(tmp_path / "elsewhere")
    assert meetings_root(config) == tmp_path / "elsewhere"


# ---------------------------------------------------------------------------
# Presence: one Target captions the microphone
# ---------------------------------------------------------------------------

def test_presence_focus_then_earliest_meeting(tmp_path):
    one, two, three = (Presence(n, tmp_path) for n in ("target1", "target2", "target3"))
    assert one.mic_owner() is None
    one.beat(True, 100.0)
    two.beat(True, 50.0)
    three.beat(False, None)
    assert one.mic_owner() == "target2"          # nobody focused: the earlier meeting
    one.touch_focus()
    assert two.mic_owner() == "target1"          # the Target the user operated last
    three.touch_focus()                          # focus on a Target without a meeting
    assert one.mic_owner() == "target2"
    old = json.loads((tmp_path / "target2.json").read_text(encoding="utf-8"))
    old["at"] = time.time() - 60                 # crashed process: stale heartbeat
    (tmp_path / "target2.json").write_text(json.dumps(old), encoding="utf-8")
    assert one.mic_owner() == "target1"


def test_caption_mic_follows_focus():
    service = CaptionService(object(), "http://127.0.0.1:9", mic=lambda: None, mic_name="EarPods")
    service.on = True
    mic = service._links["me"]
    assert service._wanted(mic)
    service.mic_focus = False
    assert not service._wanted(mic) and service._wanted(service._links["remote"])


def test_hub_limit_is_not_reported_as_a_wrong_key():
    service = CaptionService(object(), "http://127.0.0.1:9")

    class Rejected(Exception):
        def __init__(self, code):
            self.response = type("R", (), {"status_code": code})()
    assert "full" in service._describe(Rejected(503))
    assert "limit" in service._describe(Rejected(403))
    assert "rejected the key" in service._describe(Rejected(401))


# ---------------------------------------------------------------------------
# Daily caption files
# ---------------------------------------------------------------------------

def _epoch(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=JST).timestamp()


def _caption_session(recordings, sid, rows, finalized=True):
    folder = recordings / ".captions"
    (folder / sid).mkdir(parents=True, exist_ok=True)
    with (folder / f"{sid}.jsonl").open("w", encoding="utf-8") as f:
        for at, speaker, text in rows:
            f.write(json.dumps({"at": int(at * 1000), "text": text, "speaker": speaker}, ensure_ascii=False) + "\n")
    meta = {"id": sid, "started_at": rows[0][0], "first_at": rows[0][0], "last_at": rows[-1][0],
            "finals": len(rows), "finalized": time.time() if finalized else None}
    (folder / sid / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_daily_markdown(tmp_path):
    day = date(2026, 11, 9)
    recordings, meetings = tmp_path / "recordings", tmp_path / "meetings"
    _caption_session(recordings, "20261109-100200-aaaaaaaa", [
        (_epoch(day, 10, 2, 15), "remote", "おはようございます"),
        (_epoch(day, 10, 2, 20), "me", "よろしくお願いします"),
        (_epoch(day, 10, 58, 0), "remote", "以上です")])
    _caption_session(recordings, "20261109-230000-bbbbbbbb", [
        (_epoch(day, 23, 50), "remote", "夜の会議")], finalized=False)
    (recordings / ".captions" / "settings.json").write_text('{"cc": true}', encoding="utf-8")
    audio = meetings / "audio" / "2026-11-09"
    audio.mkdir(parents=True)
    (audio / "2026-11-09_100155.mka").write_bytes(b"x")
    (audio / "2026-11-09_100155.json").write_text(json.dumps(
        {"start": _epoch(day, 10, 1, 55), "end": _epoch(day, 11, 1, 0)}), encoding="utf-8")
    (meetings / ".backfill").mkdir(parents=True)
    (meetings / ".backfill" / "2026-11-09_100155.jsonl").write_text(json.dumps(
        {"at": int(_epoch(day, 10, 30) * 1000), "text": "補った行", "speaker": "remote", "source": "batch"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    # captured while the hub was down: audio only
    (audio / "2026-11-09_140000.mka").write_bytes(b"x")
    (audio / "2026-11-09_140000.json").write_text(json.dumps(
        {"start": _epoch(day, 14, 0), "end": _epoch(day, 14, 20)}), encoding="utf-8")
    recordings.mkdir(exist_ok=True)
    video = recordings / "recording-2026-11-09T10-15-00-000-JST-12345678.mp4"
    video.write_bytes(b"x")
    import os
    os.utime(video, (_epoch(day, 10, 40), _epoch(day, 10, 40)))

    daily = DailyCaptions(recordings, meetings, "target1")
    path = daily.write(day)
    text = path.read_text(encoding="utf-8")
    assert path == meetings / "2026-11-09.md"
    assert text.startswith("# 2026-11-09 会议字幕 · target1")
    assert "## 10:02–10:58（56 分钟）" in text
    assert "- [10:02:15] 对方：おはようございます" in text and "- [10:02:20] 我：よろしくお願いします" in text
    assert "- [10:30:00] 补·对方：補った行" in text
    assert "- 录音：[2026-11-09_100155.mka](audio/2026-11-09/2026-11-09_100155.mka)" in text
    assert "../recordings/recording-2026-11-09T10-15-00-000-JST-12345678.mp4" in text
    assert "## 14:00–14:20（20 分钟）" in text and "没有字幕" in text
    assert "## 23:50–（进行中）" in text
    assert text.index("10:02–") < text.index("14:00–") < text.index("23:50–")
    assert daily.days() == [day]
    view = daily.view(day)          # the viewer's 今天 tab
    assert view["type"] == "captions_day" and view["date"] == "2026-11-09" and len(view["meetings"]) == 3
    first = view["meetings"][0]
    assert first["rows"][0] == [int(_epoch(day, 10, 2, 15) * 1000), "remote", "おはようございます", False]
    assert [r for r in first["rows"] if r[3]] == [[int(_epoch(day, 10, 30) * 1000), "remote", "補った行", True]]
    assert view["meetings"][2]["live"] is True
    assert daily.view(date(2026, 11, 20))["meetings"] == []
    assert daily.write_missing(today=date(2026, 11, 10)) == []      # exists already
    path.unlink()
    assert daily.write_missing(today=date(2026, 11, 10)) == [path]
    # CC off again (an automatic session ended): the still-resumable session gets its end time
    (recordings / ".captions" / "settings.json").write_text('{"cc": false}', encoding="utf-8")
    assert "## 23:50–23:50（0 分钟）" in daily.render(day) and "进行中" not in daily.render(day)


# ---------------------------------------------------------------------------
# Capture sessions with a real ffmpeg
# ---------------------------------------------------------------------------

class FakeCaptions:
    available, on, on_since = True, False, None

    def __init__(self):
        self.preroll = None

    def set_preroll(self, chunks):
        self.preroll = chunks

    def remote_state(self):
        return "live" if self.on else "off"

    def set_mic_focus(self, focus):
        self.focus = focus


class FakeMeeting:
    def __init__(self, captions):
        self.captions, self.calls = captions, []

    def set_cc(self, on, reason="viewer", persist=True):
        self.calls.append((on, persist))
        self.captions.on = on


def _streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return out.split()


@needs_ffmpeg
def test_auto_session_records_three_tracks_and_switches_cc(tmp_path):
    audio = FakeAudio()
    captions = FakeCaptions()
    meeting = FakeMeeting(captions)
    days = []

    async def run():
        cap = AutoCapture(audio, tmp_path, "target1", meeting, captions, threshold_db=-55, silence=1.5,
                          on_day_changed=days.append)
        cap.start(asyncio.get_running_loop())
        await asyncio.sleep(0.5)
        assert cap.session is None
        audio.loud = True
        for _ in range(100):                 # 3 s of sound starts a session
            if cap.session is not None:
                break
            await asyncio.sleep(0.1)
        assert cap.session and cap.session["trigger"] == "auto"
        assert meeting.calls == [(True, False)] and captions.preroll   # CC on, not persisted, pre-roll handed over
        await asyncio.sleep(1.0)
        audio.loud = False
        for _ in range(100):                 # 1.5 s of silence (test setting) ends it
            if cap.session is None and len(meeting.calls) == 2:
                break
            await asyncio.sleep(0.1)
        assert cap.session is None
        assert meeting.calls[-1] == (False, False)
        await cap.close()

    try:
        asyncio.run(run())
    finally:
        audio.close()
    files = list((tmp_path / "audio").glob("*/*.mka"))
    assert len(files) == 1
    side = json.loads(files[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert side["trigger"] == "auto" and side["end"] and side["reason"] == "silence" and side["preroll"] > 2
    assert _streams(files[0]) == ["audio", "audio", "audio"]
    assert days and all(isinstance(d, date) for d in days)


@needs_ffmpeg
def test_manual_stop_in_auto_mode_skips_the_rest_of_the_meeting(tmp_path):
    audio = FakeAudio()

    async def run():
        cap = AutoCapture(audio, tmp_path, "target1", silence=60)
        cap.start(asyncio.get_running_loop())
        audio.loud = True
        for _ in range(200):
            if cap.session is not None:
                break
            await asyncio.sleep(0.1)
        assert cap.session is not None, "3 s of sound did not start a session"
        await cap.stop("manual")
        assert cap.snoozed and cap.state()["snoozed"]
        await asyncio.sleep(0.5)
        assert cap.session is None             # sound goes on, nothing restarts
        await cap.set_mode("manual")
        await cap.begin("manual")
        assert cap.session["trigger"] == "manual"
        await cap.set_mode("off")              # off stops the running session
        assert cap.session is None
        await cap.close()
        assert json.loads((tmp_path / ".capture.json").read_text(encoding="utf-8"))["mode"] == "off"

    try:
        asyncio.run(run())
    finally:
        audio.close()
    assert len(list((tmp_path / "audio").glob("*/*.mka"))) == 2
    assert AutoCapture(None, tmp_path, "target1").mode == "off"     # the mode survives a restart


def test_unfinished_sidecar_is_closed_at_start(tmp_path):
    folder = tmp_path / "audio" / "2026-11-09"
    folder.mkdir(parents=True)
    (folder / "2026-11-09_100000.json").write_text(json.dumps({"start": 1.0, "end": None}), encoding="utf-8")
    (folder / "2026-11-09_100000.mka").write_bytes(b"x")
    cap = AutoCapture(None, tmp_path, "target1")
    cap._recover()
    data = json.loads((folder / "2026-11-09_100000.json").read_text(encoding="utf-8"))
    assert data["end"] and data["reason"] == "recovered"


def test_backfill_gaps_become_batch_rows(tmp_path, monkeypatch):
    class Hub:
        def submit(self, path, language):
            return "job1"

        def status(self, job):
            return {"status": "done"}

        def result(self, job):
            return {"segments": [{"start": 1.0, "end": 2.0, "text": "取り戻した"}]}

        def delete(self, job):
            pass
    monkeypatch.setattr(_auto_capture.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(_auto_capture.shutil, "which", lambda name: "ffmpeg")
    done = []
    backfill = _auto_capture.Backfill(tmp_path, Hub(), on_done=done.append)
    job = backfill.enqueue(tmp_path / "audio" / "d" / "2026-11-09_100000.mka", 1000.0, [(10.0, 20.0), (30, 31)])
    assert backfill.process(job)
    rows = [json.loads(l) for l in (tmp_path / ".backfill" / "2026-11-09_100000.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == [{"at": 1009000, "end": 1010000, "text": "取り戻した", "lang": "", "speaker": "remote", "source": "batch"}]
    assert done == [1000.0] and not job.exists()   # the 1 s gap was too short to queue


# ---------------------------------------------------------------------------
# Viewer protocol
# ---------------------------------------------------------------------------

from tests.test_captions import _DummyHardware, _free_port, _read  # noqa: E402


@needs_ffmpeg
def test_viewer_meeting_button_protocol(tmp_path):
    from serial_hid_kvm._web_viewer import WebViewerServer
    audio = FakeAudio()

    async def run():
        config = Config()
        config.capture_enabled = True
        config.capture_mode = "manual"
        config.web_port = _free_port()
        config.target_name = "target9"
        config.recording_dir = str(tmp_path / "recordings")
        server = WebViewerServer(_DummyHardware(), config, audio=audio)
        server._cap_label = "dummy"
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{config.web_port}/ws") as ws:
                hello = await _read(ws, "hello")
                cap = hello["capture"]
                assert cap["available"] and cap["mode"] == "manual" and not cap["active"]
                assert cap["dir"] == str(tmp_path / "meetings") and cap["target"] == "target9"
                await ws.send(json.dumps({"type": "capture", "action": "start"}))
                while not (state := await _read(ws, "capture_state"))["active"]:
                    pass
                assert state["trigger"] == "manual" and state["file"].endswith(".mka")
                assert "level" in await _read(ws, "capture_level")
                await ws.send(json.dumps({"type": "capture", "action": "stop"}))
                while (await _read(ws, "capture_state"))["active"]:
                    pass
                await ws.send(json.dumps({"type": "capture", "action": "mode", "mode": "bogus"}))
                assert "auto / manual / off" in (await _read(ws, "capture_state"))["notice"]
                await ws.send(json.dumps({"type": "captions_day"}))
                today = await _read(ws, "captions_day")
                assert today["date"] == datetime.now(JST).date().isoformat() and today["meetings"]
                assert today["meetings"][0]["rows"] == []      # recorded, no captions (no hub in tests)
                await ws.send(json.dumps({"type": "captions_day", "date": "not-a-date"}))
                assert "error" in await _read(ws, "captions_day")
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
    assert len(list((tmp_path / "meetings" / "audio").glob("*/*.mka"))) == 1
    assert list((tmp_path / "meetings").glob("*.md"))      # today's file, written at stop
