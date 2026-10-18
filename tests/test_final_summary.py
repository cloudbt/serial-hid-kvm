"""Final meeting-points summary in the background when CC is turned off."""
import asyncio
import json
import threading
import time

import websockets

from serial_hid_kvm._live_meeting import LiveMeeting
from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config
from tests.mock_ai_hub import MockHub
from tests.test_captions import _Audio, _read
from tests.test_live_meeting import _Captions, _Minutes, _final, _live
from tests.test_web_auth import _free_port


def _meeting(tmp_path, runner, interval=5):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes, interval)
    meeting.set_interval(interval)
    meeting.runners = {name: runner for name in ("agy", "codex", "mac-qwen")}
    meeting.ensure_session()
    return meeting, captions, minutes


def _summaries(tmp_path, meeting):
    folder = tmp_path / ".captions" / meeting.session["id"]
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("summary-*.json"))]


async def _settle(meeting):
    for _ in range(200):
        if meeting._final_task is None or meeting._final_task.done():
            if not meeting.running:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("final summary did not finish")


def test_cc_off_runs_final_even_with_interval_off(tmp_path):
    calls = []
    meeting, captions, _ = _meeting(tmp_path, lambda w, p, t: calls.append(t) or _live(), interval=0)

    async def run():
        _final(meeting, "最後の発言")
        captions.active = False
        meeting.on_captions_stopped()
        assert json.loads((tmp_path / ".captions" / meeting.session["id"] / "meta.json").read_text())["final_pending"]
        await _settle(meeting)

    asyncio.run(run())
    [record] = _summaries(tmp_path, meeting)
    assert record["final"] is True and "最後の発言" in calls[0]
    assert meeting.session["final_pending"] is False
    message = meeting.message()
    assert message["final"] is True and message["state"] == "idle" and message["data"]
    states = [(m["state"], m["final"]) for m in captions.sent]
    assert ("running", True) in states and states[-1] == ("idle", True)


def test_nothing_new_or_summarizer_off_means_no_final(tmp_path):
    meeting, captions, _ = _meeting(tmp_path, lambda w, p, t: _live())

    async def run():
        _final(meeting, "a")
        await meeting.run(manual=True)
        captions.active = False
        meeting.on_captions_stopped()  # everything already summarized
        assert meeting._final_task is None
        _final(meeting, "b")
        meeting.set_summarizer("off")
        meeting.on_captions_stopped()
        assert meeting._final_task is None and not meeting.session.get("final_pending")

    asyncio.run(run())
    assert len(_summaries(tmp_path, meeting)) == 1


def test_final_waits_for_running_summary_then_tops_up(tmp_path):
    gate, started, calls = threading.Event(), threading.Event(), []

    def runner(work, prompt, text):
        calls.append(text)
        if len(calls) == 1:
            started.set()
            gate.wait(5)
        return _live()

    meeting, captions, _ = _meeting(tmp_path, runner)

    async def run():
        _final(meeting, "一つ目")
        regular = asyncio.ensure_future(meeting.run(manual=True))
        await asyncio.to_thread(started.wait, 5)
        _final(meeting, "二つ目（通常の総括中に到着）")
        captions.active = False
        meeting.on_captions_stopped()
        await asyncio.sleep(0.1)
        assert len(calls) == 1  # never concurrent
        gate.set()
        assert await regular is True
        await _settle(meeting)

    asyncio.run(run())
    records = _summaries(tmp_path, meeting)
    assert [r["final"] for r in records] == [False, True]
    assert "二つ目" in calls[1] and records[1]["finals"] == 2


def test_reopening_cc_during_final_keeps_it_as_an_ordinary_summary(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def runner(work, prompt, text):
        started.set()
        gate.wait(5)
        return _live()

    meeting, captions, _ = _meeting(tmp_path, runner)

    async def run():
        _final(meeting, "x")
        captions.active = False
        meeting.on_captions_stopped()
        await asyncio.to_thread(started.wait, 5)
        captions.active = True       # CC back on while the final summary runs
        meeting.on_subscribe()        # same session continues
        _final(meeting, "y")
        gate.set()
        await _settle(meeting)

    asyncio.run(run())
    [record] = _summaries(tmp_path, meeting)
    assert record["final"] is False and not meeting.session["final_pending"]
    assert meeting.session["finals"] == 2 and meeting.session["summarized_finals"] == 1


def test_pending_final_resumes_after_restart(tmp_path):
    meeting, captions, minutes = _meeting(tmp_path, lambda w, p, t: _live())
    _final(meeting, "再起動前の発言")
    meeting._save(final_pending=True)  # process died before the final summary ran
    sid = meeting.session["id"]

    restarted = LiveMeeting(tmp_path, _Captions(), minutes)
    restarted.captions.active = False
    restarted.runners = {name: (lambda w, p, t: _live()) for name in ("agy", "codex", "mac-qwen")}

    async def run():
        task = restarted.resume_pending()
        assert task is not None and restarted.session["id"] == sid
        await task

    asyncio.run(run())
    [record] = _summaries(tmp_path, restarted)
    assert record["final"] is True and restarted.session["final_pending"] is False


def test_finalize_waits_for_running_final_and_closed_view_survives_restart(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def runner(work, prompt, text):
        started.set()
        gate.wait(5)
        return _live()

    meeting, captions, minutes = _meeting(tmp_path, runner)
    sid = meeting.session["id"]

    async def run():
        for n in range(12):
            _final(meeting, f"発言{n}", int((time.time() - 1200 + n * 30) * 1000))
        captions.active = False
        meeting.on_captions_stopped()
        await asyncio.to_thread(started.wait, 5)
        meeting.session["last_at"] = meeting.session["seen_at"] = time.time() - 3600  # window long over
        await meeting.tick()
        assert meeting.session is not None and not json.loads(
            (tmp_path / ".captions" / sid / "meta.json").read_text())["finalized"]
        gate.set()
        await _settle(meeting)
        meeting.session["last_at"] = meeting.session["seen_at"] = time.time() - 3600
        meeting.session["first_at"] = meeting.session["last_at"] - 600  # a 10-minute meeting
        await meeting.tick()
        assert meeting.session is None

    asyncio.run(run())
    meta = json.loads((tmp_path / ".captions" / sid / "meta.json").read_text())
    assert meta["finalized"] and meta["summaries"] == 1 and len(minutes.queued) == 1  # auto minutes unchanged
    closed = meeting.message()
    assert closed["session"]["closed"] is True and closed["final"] is True and closed["data"]
    again = LiveMeeting(tmp_path, _Captions(), minutes).message()  # after a restart, CC off
    assert again["session"]["id"] == sid and again["final"] is True


def test_failed_final_shows_failure_and_retry_runs_final_again(tmp_path):
    outcomes = [RuntimeError("down")] * 3 + [None]

    def runner(work, prompt, text):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return _live()

    meeting, captions, _ = _meeting(tmp_path, runner)

    async def run():
        _final(meeting, "x")
        captions.active = False
        meeting.on_captions_stopped()
        await _settle(meeting)
        failed = meeting.message()
        assert failed["state"] == "failed" and failed["final"] is True and failed["session"]["final_pending"]
        await meeting.request_run()  # 重试 with CC off
        await _settle(meeting)

    asyncio.run(run())
    [record] = _summaries(tmp_path, meeting)
    assert record["final"] is True and meeting.message()["state"] == "idle"


def test_viewer_gets_final_summary_after_cc_off(tmp_path):
    hub = MockHub(caption_every=0.3).start()
    audio = _Audio(speech=False)

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        config.captions_summary_interval = 0
        server = WebViewerServer(_DummyHardwareStub(), config, audio=audio)
        server._cap_label = "dummy"
        server._captions.LINGER = 0.1
        server._meeting.runners = {n: (lambda w, p, t: _live()) for n in ("agy", "codex", "mac-qwen")}
        await server.start()
        url = f"ws://127.0.0.1:{config.web_port}/ws"
        try:
            async with websockets.connect(url) as ws:
                await _read(ws, "hello")
                await ws.send(json.dumps({"type": "captions", "on": True}))
                await _read(ws, "caption")
                await _read(ws, "caption")
                await ws.send(json.dumps({"type": "captions", "on": False}))
            # The viewer is gone; the server still makes the final summary.
            for _ in range(100):
                if server._meeting.last and server._meeting.last.get("final"):
                    break
                await asyncio.sleep(0.1)
            assert server._meeting.last["final"] is True
            async with websockets.connect(url) as ws:  # a later page load, CC still off
                await _read(ws, "hello")
                msg = await _read(ws, "live_summary")
                assert msg["final"] is True and msg["data"]["current_topic"] == "リリース計画"
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()


class _DummyHardwareStub:
    def __init__(self):
        from tests.test_web_auth import _DummyHardware
        self._hw = _DummyHardware()

    def get_capture(self):
        return self._hw.get_capture()

    def get_ch9329(self):
        return self._hw.get_ch9329()
