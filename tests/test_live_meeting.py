"""Live meeting points, CC sessions (jsonl/resume), interim captions, caption-only minutes."""
import asyncio
import json
import time

import pytest
import websockets

from serial_hid_kvm._captions import CaptionService
from serial_hid_kvm._live_meeting import LIVE_SCHEMA, LiveMeeting, validate_live
from serial_hid_kvm._minutes import MinutesService, captions_transcript
from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config
from tests.mock_ai_hub import MockHub
from tests.test_captions import _Audio, _next, _read
from tests.test_web_auth import _DummyHardware, _free_port


def _live(points=("API 完成",), new=("API 完成",)):
    return {"current_topic": "リリース計画", "points": list(points), "decisions": ["金曜リリース"],
            "todos": [{"task": "手順書", "owner": "", "due": None}], "open_questions": ["移行時期"],
            "new_since_last": list(new)}


class _Captions:
    """Minimal CaptionService stand-in for LiveMeeting."""

    def __init__(self):
        self.active = True
        self.sent = []
        self._history = []
        self.on_final = self.on_subscribe = None

    def broadcast(self, message):
        self.sent.append(message)

    def clear_history(self):
        self._history.clear()


class _Minutes:
    default = "agy"
    order = ["agy", "codex", "mac-qwen"]
    agy = codex = None
    mac = ("", None, "current")
    timeout = 60

    def __init__(self):
        self.queued = []

    def enqueue_captions(self, *args):
        self.queued.append(args)
        return {"id": "9" * 32}

    def view(self, key):
        return {"id": key, "state": "queued", "label": "排队中", "links": {}}


def _final(meeting, text, at=None):
    at = at or int(time.time() * 1000)
    meeting.add_final({"type": "caption", "seq": 1, "at": at, "end": at + 1500, "text": text, "lang": "ja"})


def test_validate_live_schema():
    data = validate_live({**_live(), "extra": 1})
    assert data["todos"] == [{"task": "手順書", "owner": None, "due": None}]
    assert set(LIVE_SCHEMA["required"]) == set(data)
    with pytest.raises(ValueError):
        validate_live({k: v for k, v in _live().items() if k != "new_since_last"})
    with pytest.raises(ValueError):
        validate_live({**_live(), "points": "x"})


def test_session_jsonl_resume_and_finalize(tmp_path):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes)
    assert captions.on_final == meeting.add_final
    [hello] = meeting.on_subscribe()
    sid = hello["session"]["id"]
    assert hello["type"] == "live_summary" and hello["interval"] == 5 and hello["summarizer"] == "agy"
    start = time.time() * 1000
    for n in range(12):
        _final(meeting, f"発言 {n}", int(start + n * 15000))
    rows = (tmp_path / ".captions" / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 12 and json.loads(rows[3])["text"] == "発言 3"
    transcript = captions_transcript(tmp_path / ".captions" / f"{sid}.jsonl", meeting.session["started_at"])
    assert transcript["segments"][1]["start"] - transcript["segments"][0]["start"] == pytest.approx(15, abs=0.01)

    # "Restart" within the resume window continues the same session and replays recent lines.
    again = LiveMeeting(tmp_path, _Captions(), minutes)
    assert again.ensure_session()["id"] == sid and again.session["finals"] == 12
    assert len(again.captions._history) == 12

    # After the window the stale session is finalized; unrecorded + long enough -> caption minutes.
    meta = tmp_path / ".captions" / sid / "meta.json"
    state = json.loads(meta.read_text(encoding="utf-8"))
    state["last_at"] = state["seen_at"] = time.time() - 3600
    state["first_at"] = state["last_at"] - 300
    meta.write_text(json.dumps(state), encoding="utf-8")
    later = LiveMeeting(tmp_path, _Captions(), minutes)
    assert later.ensure_session()["id"] != sid
    assert json.loads(meta.read_text(encoding="utf-8"))["finalized"]
    (jsonl, stem, chosen, started, session), = minutes.queued
    assert stem == "meeting-" + sid and chosen == "agy" and session == sid


def test_recorded_or_short_sessions_get_no_auto_minutes(tmp_path):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes)
    meeting.ensure_session()
    for n in range(20):
        _final(meeting, f"x{n}", int((time.time() - 600 + n * 30) * 1000))
    meeting.note_recording()
    meeting.new_meeting()  # recorded: minutes come from the recording
    meeting.ensure_session()
    _final(meeting, "short")
    meeting.new_meeting()  # too short
    assert minutes.queued == []
    assert captions.sent[-1]["session"]["finals"] == 0  # new meeting announced to viewers


def test_manual_minutes_cover_session_unless_captions_grew(tmp_path):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes)
    meeting.ensure_session()
    for n in range(12):
        _final(meeting, f"x{n}", int((time.time() - 600 + n * 30) * 1000))
    meeting.generate_minutes()
    assert len(minutes.queued) == 1 and captions.sent[-1]["minutes"]["id"] == "9" * 32
    meeting.new_meeting()
    assert len(minutes.queued) == 1  # nothing new since 生成会议记录
    meeting.ensure_session()
    for n in range(12):
        _final(meeting, f"y{n}", int((time.time() - 600 + n * 30) * 1000))
    meeting.generate_minutes()
    _final(meeting, "その後の発言")
    meeting.new_meeting()
    assert len(minutes.queued) == 3  # grew after the manual run -> regenerated at the end


def test_live_summary_run_fallback_skip_and_interval(tmp_path):
    captions, minutes = _Captions(), _Minutes()
    meeting = LiveMeeting(tmp_path, captions, minutes)
    calls = []

    def failing(name):
        def run(work, prompt, text):
            calls.append((name, prompt, text))
            raise RuntimeError(name + " down")
        return run

    def good(work, prompt, text):
        calls.append(("codex", prompt, text))
        return _live()

    meeting.runners = {"agy": failing("agy"), "codex": good, "mac-qwen": failing("mac-qwen")}

    async def run():
        meeting.ensure_session()
        assert not meeting.due()  # no captions yet
        with pytest.raises(ValueError):
            await meeting.run(manual=True)
        _final(meeting, "では始めます")
        assert not meeting.due()  # first automatic summary only one interval after the start
        meeting.last_run -= 301
        assert meeting.due()
        assert await meeting.run() is True
        assert [c[0] for c in calls] == ["agy", "codex"]
        prompt = calls[1][1]
        assert "untrusted DATA" in prompt and "user's own speech is" in prompt and "[00:00:00] では始めます" in calls[1][2]
        states = [m["state"] for m in captions.sent]
        assert states[-2:] == ["running", "idle"] and captions.sent[-1]["used"] == "codex"
        assert captions.sent[-1]["data"]["current_topic"] == "リリース計画"
        assert not meeting.due()  # nothing new since the last summary
        saved = list((tmp_path / ".captions" / meeting.session["id"]).glob("summary-*.json"))
        assert len(saved) == 1 and json.loads(saved[0].read_text(encoding="utf-8"))["used"] == "codex"
        _final(meeting, "次の議題")
        assert not meeting.due()  # interval not elapsed
        meeting.last_run -= 301
        assert meeting.due()
        meeting.running = True
        assert await meeting.run() is False  # never concurrent
        meeting.running = False
        meeting.set_interval(0)
        assert not meeting.due()
        assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["interval"] == 0
        with pytest.raises(ValueError):
            meeting.set_interval(7)
        meeting.set_interval(10)
        assert LiveMeeting(tmp_path, _Captions(), minutes).interval == 10
        # Mac Qwen stays on the LAN: no cloud fallback for live points either.
        meeting.set_summarizer("mac-qwen")
        calls.clear()
        assert await meeting.run(manual=True) is False
        assert [c[0] for c in calls] == ["mac-qwen"] and captions.sent[-1]["state"] == "failed"
        meeting.set_summarizer("off")
        with pytest.raises(ValueError):
            await meeting.run(manual=True)

    asyncio.run(run())


def test_interim_captions_are_overlay_only(tmp_path):
    service = CaptionService(_Audio(speech=False), "http://127.0.0.1:9")
    service._audio.close()
    finals, vtt = [], tmp_path / "r.live.vtt"
    service.on_final = finals.append
    service.begin_recording("s", vtt, time.time() - 10)

    async def run():
        q = asyncio.Queue()
        service._subscribers.add(q)
        service._epoch = time.time() - 5
        for n in range(30):
            service._caption({"type": "caption", "seq": 7, "t0": 1, "t1": 1 + n / 10, "text": "途中" * (n + 1),
                              "final": False})
        service._caption({"type": "caption", "seq": 7, "t0": 1, "t1": 4, "text": "完成した文", "final": True})
        sent = [q.get_nowait() for _ in range(q.qsize())]
        assert [m["final"] for m in sent] == [False] * 30 + [True]
        assert {m["seq"] for m in sent} == {7}

    asyncio.run(run())
    assert [f["text"] for f in finals] == ["完成した文"]
    assert [h["text"] for h in service._history] == ["完成した文"]
    assert vtt.read_text(encoding="utf-8").count("-->") == 1


def test_mock_hub_interim_flow(tmp_path):
    hub = MockHub(caption_every=1.5, interim_every=0.3).start()
    audio = _Audio(speech=False)

    async def run():
        service = CaptionService(audio, hub.url)
        q = service.subscribe()
        first = await _next(q, "caption")
        seen = [first]
        while seen[-1]["final"] is False or len(seen) < 3:
            seen.append(await _next(q, "caption"))
        await service.close()
        return seen

    try:
        seen = asyncio.run(run())
    finally:
        audio.close()
        hub.stop()
    interim = [m for m in seen if not m["final"]]
    final = [m for m in seen if m["final"]]
    assert interim and final and interim[-1]["seq"] == final[0]["seq"]


def test_caption_only_minutes_job(tmp_path):
    config = Config()
    config.recording_dir = str(tmp_path)
    library = RecordingLibrary(tmp_path)
    service = MinutesService(config, library)
    captured = []

    def runner(job, folder, prompt, transcript, transcript_path, duration):
        captured.append(prompt)
        return {"title": "字幕だけの会議", "summary": "相手の発言のみ", "topics": [{"title": "t", "points": ["p"], "start_sec": 30}],
                "decisions": [], "todos": [], "open_questions": [], "uncertainties": []}

    service.runners = {"agy": runner}
    jsonl = tmp_path / ".captions" / "20261018-100000-abcd1234.jsonl"
    jsonl.parent.mkdir(parents=True)
    base = 1_800_000_000_000
    jsonl.write_text("\n".join(json.dumps({"seq": n, "at": base + n * 30000, "end": base + n * 30000 + 2000,
                                           "text": f"発言{n}", "lang": "ja"}, ensure_ascii=False) for n in range(5))
                     + "\n{broken\n", encoding="utf-8")
    job = service.enqueue_captions(jsonl, "meeting-20261018-100000-abcd1234", "agy", base / 1000, "abcd1234")
    service.process(job)
    job = service.load(job["id"])
    assert job["status"] == "done" and job["kind"] == "captions"
    assert "remote side only" in captured[0]
    md = (tmp_path / "meeting-notes" / "meeting-20261018-100000-abcd1234.minutes.md").read_text(encoding="utf-8")
    assert "相手側" in md and "`00:00:30`" in md and ".mp4" not in md
    assert (tmp_path / "meeting-notes" / "meeting-20261018-100000-abcd1234.srt").is_file()
    [note] = service.notes()
    assert note["minutes"]["state"] == "done" and set(note["minutes"]["links"]) == {"minutes", "minutes_md", "transcript"}
    from urllib.parse import parse_qs, urlsplit
    path = library.resolve_link(parse_qs(urlsplit(note["minutes"]["links"]["minutes"]).query), suffixes=(".md",))
    page = service.page(path, "n")
    assert "<video" not in page and "相手側" in page and "発言3" in page
    # Re-generating rebuilds from the (grown) jsonl.
    again = service.enqueue_captions(jsonl, "meeting-20261018-100000-abcd1234", "agy", base / 1000, "abcd1234")
    assert again["status"] == "queued" and not (service._dir(again["id"]) / "transcript.json").exists()


def test_viewer_live_summary_protocol(tmp_path):
    hub = MockHub(caption_every=0.4).start()
    audio = _Audio(speech=False)

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        server = WebViewerServer(_DummyHardware(), config, audio=audio)
        server._cap_label = "dummy"
        server._meeting.runners = {name: (lambda work, prompt, text: _live()) for name in ("agy", "codex", "mac-qwen")}
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{config.web_port}/ws") as ws:
                await _read(ws, "hello")
                connect = await _read(ws, "live_summary")  # sent to every viewer, CC off
                assert connect["session"] is None and connect["final"] is False
                await ws.send(json.dumps({"type": "captions", "on": True}))
                first = await _read(ws, "live_summary")
                assert first["session"]["id"] and first["data"] is None
                await ws.send(json.dumps({"type": "live_summary", "action": "set_summarizer", "summarizer": "codex"}))
                assert (await _read(ws, "live_summary"))["summarizer"] == "codex"
                await _read(ws, "caption")
                await ws.send(json.dumps({"type": "live_summary", "action": "set_interval", "minutes": 3}))
                assert (await _read(ws, "live_summary"))["interval"] == 3
                await ws.send(json.dumps({"type": "live_summary", "action": "run"}))
                while True:
                    msg = await _read(ws, "live_summary")
                    if msg["state"] == "idle" and msg["data"]:
                        break
                assert msg["used"] == "codex" and msg["data"]["decisions"] == ["金曜リリース"]
                await ws.send(json.dumps({"type": "live_summary", "action": "minutes"}))
                msg = await _read(ws, "live_summary")
                assert msg["minutes"]["kind"] == "captions"
                await ws.send(json.dumps({"type": "live_summary", "action": "bogus"}))
                assert "未知" in (await _read(ws, "live_summary"))["notice"]
                old = msg["session"]["id"]
                await ws.send(json.dumps({"type": "captions_new_meeting"}))
                while True:
                    msg = await _read(ws, "live_summary")
                    if msg["session"]["id"] != old:
                        break
                assert msg["data"] is None
                await ws.send(json.dumps({"type": "recordings_list"}))
                listing = await _read(ws, "recordings_list")
                assert listing["meeting_notes"][0]["minutes"]["kind"] == "captions"
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()
