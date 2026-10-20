"""The user's own voice: host microphone as a second caption stream, JST times, auto new meeting."""
import asyncio
import json
import time

import websockets

from serial_hid_kvm import _web_viewer
from serial_hid_kvm._captions import CaptionService
from serial_hid_kvm._live_meeting import LiveMeeting
from serial_hid_kvm._minutes import (MinutesService, SPEAKERS_NOTE, captions_transcript, markdown_minutes,
                                     recording_origin, srt_text, transcript_lines)
from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm.config import Config
from tests.mock_ai_hub import MockHub
from tests.test_captions import _Audio, _next, _read
from tests.test_live_meeting import _Captions, _Minutes, _live
from tests.test_web_auth import _DummyHardware, _free_port


class _Mic(_Audio):
    """Opened on demand like the real host microphone; stop() releases it."""
    opened = 0
    stopped = 0

    def __init__(self):
        super().__init__(speech=True)
        type(self).opened += 1

    def stop(self):
        type(self).stopped += 1
        self.close()


def test_two_hub_streams_carry_their_speaker_and_the_mic_can_be_switched_off():
    hub = MockHub(caption_every=0.5).start()
    hdmi = _Audio(speech=False)
    _Mic.opened = _Mic.stopped = 0

    async def run():
        service = CaptionService(hdmi, hub.url, mic=_Mic, mic_name="EarPods")
        assert service.mic_available and service.mic_on
        q = service.subscribe()
        service.set_on(True)
        speakers = set()
        while speakers != {"remote", "me"}:
            speakers.add((await _next(q, "caption"))["speaker"])
        assert len(hub.caption_sessions) == 2 and _Mic.opened == 1
        state = service.state_message()
        assert state["state"] == "live" and state["mic"]["state"] == "live" and state["mic"]["name"] == "EarPods"
        assert service.set_mic(False) and not service.set_mic(False)
        for _ in range(100):  # the microphone is released, HDMI captions go on
            if _Mic.stopped:
                break
            await asyncio.sleep(0.05)
        assert _Mic.stopped == 1 and service._links["remote"].state[0] == "live"
        while True:
            msg = await _next(q, "caption")
            if msg["speaker"] == "remote" and not msg.get("replay"):
                break
        await service.close()

    try:
        asyncio.run(run())
    finally:
        hdmi.close()
        hub.stop()


def test_missing_microphone_never_stops_the_hdmi_captions():
    hub = MockHub(caption_every=0.5).start()
    hdmi = _Audio(speech=False)

    def unplugged():
        raise OSError("no input device named like 'EarPods'")

    async def run():
        service = CaptionService(hdmi, hub.url, mic=unplugged, mic_name="EarPods")
        q = service.subscribe()
        service.set_on(True)
        assert (await _next(q, "caption"))["speaker"] == "remote"
        for _ in range(100):
            if service.state_message()["mic"]["state"] == "offline":
                break
            await asyncio.sleep(0.05)
        mic = service.state_message()["mic"]
        assert mic["state"] == "offline" and "EarPods" in mic["detail"]
        assert service.state_message()["state"] == "live"
        await service.close()

    try:
        asyncio.run(run())
    finally:
        hdmi.close()
        hub.stop()


def _mic_captions():
    captions = _Captions()
    captions.mic_available = captions.mic_on = True

    def set_mic(on):
        changed, captions.mic_on = captions.mic_on != on, on
        return changed
    captions.set_mic = set_mic
    return captions


def test_meeting_saves_speakers_mic_switch_and_splits_after_a_gap(tmp_path):
    captions = _mic_captions()
    meeting = LiveMeeting(tmp_path, captions, _Minutes(), gap=30)
    meeting.set_cc(True)
    now = int(time.time() * 1000)
    meeting.add_final({"seq": 1, "at": now, "end": now + 1000, "text": "ご質問ありますか", "lang": "ja",
                       "speaker": "remote"})
    meeting.add_final({"seq": 1, "at": now + 2000, "end": now + 3000, "text": "はい、一点あります", "lang": "ja",
                       "speaker": "me"})
    first = meeting.session["id"]
    rows = [json.loads(line) for line in (tmp_path / ".captions" / f"{first}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["speaker"] for r in rows] == ["remote", "me"]

    meeting.set_mic(False)
    assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["mic"] is False
    restarted = _mic_captions()
    LiveMeeting(tmp_path, restarted, _Minutes())
    assert restarted.mic_on is False  # the switch survives a restart

    meeting.set_interval(5)
    meeting.session["last_at"] = time.time() - 29 * 60
    meeting.add_final({"seq": 2, "at": now, "end": now, "text": "続けます", "lang": "ja"})
    assert meeting.session["id"] == first  # under 30 minutes: same meeting
    meeting.session["last_at"] = time.time() - 31 * 60
    meeting.add_final({"seq": 3, "at": now, "end": now, "text": "午後の会議です", "lang": "ja"})
    assert meeting.session["id"] != first and meeting.session["finals"] == 1
    assert meeting.interval == 0  # a new meeting starts with automatic summaries off
    assert json.loads((tmp_path / ".captions" / first / "meta.json").read_text())["finalized"]


def test_transcripts_label_speakers_and_use_jst(tmp_path):
    base = 1_800_000_000  # 2027-01-15 17:00:00 JST
    jsonl = tmp_path / "m.jsonl"
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"at": base * 1000 + 1000, "end": base * 1000 + 3000, "text": "ご質問は", "lang": "ja"},  # legacy: remote
        {"at": base * 1000 + 4000, "end": base * 1000 + 6000, "text": "金曜までに出します", "lang": "ja", "speaker": "me"},
    ]) + "\n", encoding="utf-8")
    transcript = captions_transcript(jsonl, base)
    assert transcript["origin"] == base
    segments = transcript["segments"]
    assert transcript_lines(segments, base) == "[17:00:01] REMOTE: ご質問は\n[17:00:04] ME: 金曜までに出します"
    assert transcript_lines(segments) == "[00:00:01] REMOTE: ご質問は\n[00:00:04] ME: 金曜までに出します"
    assert transcript_lines(segments[:1], base) == "[17:00:01] ご質問は"  # no microphone: no labels
    srt = srt_text(segments, base, "ja")
    assert "17:00:01,000 --> 17:00:03,000\n相手: ご質問は" in srt and "自分: 金曜までに出します" in srt
    assert "我: 金曜までに出します" in srt_text(segments, base, "zh")
    data = {"title": "t", "summary": "s", "topics": [{"title": "x", "points": [], "start_sec": 4}],
            "decisions": [], "todos": [], "open_questions": [], "uncertainties": []}
    md = markdown_minutes(data, "ja", "m", None, 6, "codex", "m.srt", origin=base, speakers=True)
    assert "`17:00:04`" in md and "自分（自分のマイク）" in md and "相手側（Target の HDMI 音声）の発言のみ" not in md
    # Recording minutes: JST plus the position in the video, which the link uses.
    stem = "recording-2027-01-15T17-00-00-000-JST-abcd1234"
    assert recording_origin(stem) == base and recording_origin("other.mp4") is None
    md = markdown_minutes(data, "ja", stem, stem + ".mp4", 6, "codex", stem + ".srt", origin=recording_origin(stem))
    assert f"[17:00:04 · 00:00:04]({stem}.mp4#t=4)" in md


def test_caption_minutes_with_my_voice_tell_the_ai_who_spoke(tmp_path):
    config = Config()
    config.recording_dir = str(tmp_path)
    service = MinutesService(config, RecordingLibrary(tmp_path))
    captured = []

    def runner(job, folder, prompt, transcript, transcript_path, duration):
        captured.append((prompt, transcript))
        return {"title": "t", "summary": "s", "topics": [], "decisions": [], "todos": [], "open_questions": [],
                "uncertainties": []}

    service.runners = {"agy": runner}
    jsonl = tmp_path / ".captions" / "20270115-170000-abcd1234.jsonl"
    jsonl.parent.mkdir(parents=True)
    base = 1_800_000_000_000
    jsonl.write_text(json.dumps({"at": base, "end": base + 1000, "text": "質問です", "speaker": "remote"}) + "\n"
                     + json.dumps({"at": base + 2000, "end": base + 3000, "text": "回答します", "speaker": "me"}) + "\n",
                     encoding="utf-8")
    job = service.enqueue_captions(jsonl, "meeting-20270115-170000-abcd1234", "agy", base / 1000, "abcd1234")
    service.process(job)
    prompt, transcript = captured[0]
    assert SPEAKERS_NOTE in prompt and "remote side only" not in prompt
    assert "[00:00:02] ME: 回答します" in transcript  # minutes keep seconds-from-start for topic times
    md = (tmp_path / "meeting-notes" / "meeting-20270115-170000-abcd1234.minutes.md").read_text(encoding="utf-8")
    assert "自分（自分のマイク）" in md and "2027-01-15 17:00–17:00 JST" in md


def test_viewer_mic_switch_reaches_every_viewer(tmp_path, monkeypatch):
    hub = MockHub(caption_every=0.4).start()
    hdmi = _Audio(speech=False)
    monkeypatch.setattr(_web_viewer, "_open_mic", lambda name: _Mic())

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        server = WebViewerServer(_DummyHardware(), config, audio=hdmi)
        server._cap_label = "dummy"
        server._meeting.runners = {n: (lambda w, p, t: _live()) for n in ("agy", "codex", "mac-qwen")}
        await server.start()
        url = f"ws://127.0.0.1:{config.web_port}/ws"
        try:
            async with websockets.connect(url) as pc, websockets.connect(url) as phone:
                hello = await _read(pc, "hello")
                assert hello["captions"]["mic"]["available"] and hello["captions"]["mic"]["on"]
                await _read(phone, "hello")
                await pc.send(json.dumps({"type": "captions", "on": True}))
                speakers = set()
                while speakers != {"remote", "me"}:
                    speakers.add((await _read(phone, "caption"))["speaker"])
                await phone.send(json.dumps({"type": "captions_mic", "on": False}))
                while (await _read(pc, "captions_state"))["mic"]["on"]:
                    pass
                assert json.loads((tmp_path / ".captions" / "settings.json").read_text())["mic"] is False
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        hdmi.close()
        hub.stop()


WebViewerServer = _web_viewer.WebViewerServer
