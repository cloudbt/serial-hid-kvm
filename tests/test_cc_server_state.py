"""CC is one server-wide switch; AI summaries run only when a viewer asked for them."""
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
from tests.test_web_auth import _DummyHardware, _free_port


def _meeting(tmp_path, runner):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes)
    meeting.runners = {name: runner for name in ("agy", "codex", "mac-qwen")}
    meeting.set_cc(True)
    return meeting, captions, minutes


def _summaries(tmp_path, meeting):
    folder = tmp_path / ".captions" / meeting.session["id"]
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("summary-*.json"))]


async def _settle(meeting):
    for _ in range(200):
        if not meeting.running and not meeting._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("summary did not finish")


def test_no_ai_call_without_interval_cc_off_or_idle(tmp_path):
    calls = []
    meeting, captions, minutes = _meeting(tmp_path, lambda w, p, t: calls.append(t) or _live())

    async def run():
        for n in range(20):
            _final(meeting, f"発言{n}", int((time.time() - 1200 + n * 30) * 1000))
        meeting.last_run -= 3600
        await meeting.tick()                 # CC on, 自动：关闭
        meeting.set_cc(False)                # CC off
        await meeting.tick()
        meeting.session["last_at"] = meeting.session["seen_at"] = time.time() - 3600
        await meeting.tick()                 # session finalized after the window
        assert meeting.session is None

    asyncio.run(run())
    assert calls == [] and minutes.queued == []


def test_interval_resets_to_off_for_every_new_meeting(tmp_path):
    meeting, captions, _ = _meeting(tmp_path, lambda w, p, t: _live())
    meeting.set_interval(5)
    _final(meeting, "a")
    meeting.new_meeting()
    assert meeting.interval == 0 and captions.sent[-1]["interval"] == 0
    assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["interval"] == 0
    meeting.set_interval(3)
    # Resuming the same meeting (e.g. after a restart) keeps the interval.
    again = LiveMeeting(tmp_path, _Captions(), _Minutes())
    again.ensure_session()
    assert again.session["id"] == meeting.session["id"] and again.interval == 3


def test_cc_switch_is_saved_and_restored(tmp_path):
    meeting, captions, _ = _meeting(tmp_path, lambda w, p, t: _live())
    assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["cc"] is True
    restarted = LiveMeeting(tmp_path, _Captions(), _Minutes())
    restarted.captions.active = False
    restarted.restore_cc()
    assert restarted.captions.active and restarted.session["id"] == meeting.session["id"]
    meeting.set_cc(False)
    off = LiveMeeting(tmp_path, _Captions(), _Minutes())
    off.captions.active = False
    off.restore_cc()
    assert not off.captions.active


def test_forgotten_cc_turns_off_without_summary(tmp_path):
    calls = []
    meeting, captions, _ = _meeting(tmp_path, lambda w, p, t: calls.append(t) or _live())
    meeting.set_interval(5)
    _final(meeting, "x")
    captions.idle = meeting.IDLE_OFF + 1

    asyncio.run(meeting.tick())
    assert not captions.active and calls == []
    assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["cc"] is False


def test_manual_summary_with_cc_off_is_final(tmp_path):
    outcomes = [RuntimeError("down")] * 3 + [None]

    def runner(work, prompt, text):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return _live()

    meeting, captions, _ = _meeting(tmp_path, runner)

    async def run():
        _final(meeting, "最後の発言")
        meeting.set_cc(False)
        await meeting.request_run()          # 立即总结 with CC off: fails on every summarizer
        failed = meeting.message()
        assert failed["state"] == "failed" and failed["final"] is True
        await meeting.request_run()          # 重试
        await _settle(meeting)
        try:
            meeting.request_run()
        except ValueError as exc:
            assert "没有新的字幕" in str(exc)
        else:
            raise AssertionError("nothing new must not summarize again")

    asyncio.run(run())
    [record] = _summaries(tmp_path, meeting)
    assert record["final"] is True and meeting.message()["final"] is True


def test_finalize_waits_for_running_summary_and_closed_view_survives_restart(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def runner(work, prompt, text):
        started.set()
        gate.wait(5)
        return _live()

    meeting, captions, minutes = _meeting(tmp_path, runner)
    sid = meeting.session["id"]

    async def run():
        _final(meeting, "発言")
        meeting.set_cc(False)
        meeting.request_run()
        await asyncio.to_thread(started.wait, 5)
        meeting.session["last_at"] = meeting.session["seen_at"] = time.time() - 3600
        await meeting.tick()
        assert meeting.session is not None  # not while the summary runs
        gate.set()
        await _settle(meeting)
        meeting.session["last_at"] = meeting.session["seen_at"] = time.time() - 3600
        await meeting.tick()
        assert meeting.session is None

    asyncio.run(run())
    assert minutes.queued == []
    closed = LiveMeeting(tmp_path, _Captions(), minutes).message()  # after a restart, CC off
    assert closed["session"]["id"] == sid and closed["session"]["closed"] and closed["final"] is True


def test_viewers_share_cc_and_disconnects_change_nothing(tmp_path):
    hub = MockHub(caption_every=0.3).start()
    audio = _Audio(speech=False)

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        server = WebViewerServer(_DummyHardware(), config, audio=audio)
        server._cap_label = "dummy"
        calls = []
        server._meeting.runners = {n: (lambda w, p, t: calls.append(t) or _live()) for n in ("agy", "codex", "mac-qwen")}
        await server.start()
        url = f"ws://127.0.0.1:{config.web_port}/ws"
        try:
            async with websockets.connect(url) as pc:
                assert (await _read(pc, "hello"))["captions"]["on"] is False
                async with websockets.connect(url) as phone:
                    await _read(phone, "hello")
                    await pc.send(json.dumps({"type": "captions", "on": True}))
                    while not (await _read(phone, "captions_state"))["on"]:
                        pass
                    assert (await _read(phone, "caption"))["text"]
                    await phone.send(json.dumps({"type": "live_summary", "action": "set_summarizer",
                                                 "summarizer": "codex"}))
                    while (await _read(pc, "live_summary"))["summarizer"] != "codex":
                        pass
                # The phone left (tab suspended): CC stays on, the PC keeps getting captions.
                await _read(pc, "caption")
                await asyncio.sleep(0.5)
                assert server._captions.on and len(hub.caption_sessions) == 1
            # Every viewer left: still on; a new viewer sees CC on and the same summarizer.
            await asyncio.sleep(0.5)
            async with websockets.connect(url) as later:
                hello = await _read(later, "hello")
                assert hello["captions"]["on"] is True and hello["minutes"]["default"] == "codex"
                assert (await _read(later, "caption"))["text"]
                await later.send(json.dumps({"type": "captions", "on": False}))
                while (await _read(later, "captions_state"))["on"]:
                    pass
            for _ in range(60):
                if not audio.queues:
                    break
                await asyncio.sleep(0.05)
            assert audio.queues == [] and len(hub.caption_sessions) == 1
            assert calls == []  # nothing ever summarized
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()
