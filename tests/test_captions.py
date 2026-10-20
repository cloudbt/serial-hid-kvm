"""Live captions: resampler, hub session against the mock hub, viewer wiring."""
import asyncio
import json
import queue
import threading
import time

import numpy as np
import pytest
import websockets

from serial_hid_kvm._captions import BLOCK_SAMPLES, CaptionService, LiveVtt, Resampler, captions_ws_url
from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config
from tests.mock_ai_hub import MockHub
from tests.test_web_auth import _DummyHardware, _free_port


def _tone(freq, seconds, rate=48000, channels=2, amp=8000):
    t = np.arange(int(seconds * rate)) / rate
    mono = (amp * np.sin(2 * np.pi * freq * t)).astype("<i2")
    return np.repeat(mono[:, None], channels, axis=1).reshape(-1).tobytes()


def _dominant(samples, rate=16000):
    spectrum = np.abs(np.fft.rfft(samples.astype(float) * np.hanning(len(samples))))
    return np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum))]


# ---------------------------------------------------------------------------
# Resampler
# ---------------------------------------------------------------------------

def test_resampler_48k_stereo_to_16k_mono_keeps_pitch_and_length():
    pcm = _tone(1000, 2.0)
    out = Resampler(48000, 2).process(pcm)
    assert abs(len(out) - 32000) <= 1
    assert abs(_dominant(out[1000:]) - 1000) < 10
    assert 4000 < np.abs(out[1000:]).max() <= 8100


def test_resampler_chunked_equals_single_block():
    pcm = _tone(440, 1.0) + _tone(2500, 1.0)
    whole = Resampler(48000, 2).process(pcm)
    r = Resampler(48000, 2)
    pieces = []
    step = 960 * 4  # 20 ms AudioCapture blocks (2 ch x int16)
    for i in range(0, len(pcm), step + 4):  # odd, unaligned chunk sizes
        pieces.append(r.process(pcm[i:i + step + 4]))
    chunked = np.concatenate(pieces)
    assert len(chunked) == len(whole)
    assert np.abs(chunked.astype(int) - whole.astype(int)).max() <= 1


def test_resampler_rejects_aliasing_above_8k():
    out = Resampler(48000, 1).process(_tone(12000, 1.0, channels=1))
    assert np.abs(out[200:]).max() < 300  # 12 kHz would alias to 4 kHz without the low-pass


def test_resampler_44k1_and_mono_downmix():
    rate = 44100
    t = np.arange(rate) / rate
    left = (6000 * np.sin(2 * np.pi * 700 * t)).astype("<i2")
    right = -left  # opposite phase cancels in the mono mix
    stereo = np.stack([left, left], axis=1).reshape(-1).tobytes()
    out = Resampler(rate, 2).process(stereo)
    assert abs(len(out) - 16000) <= 1 and abs(_dominant(out[500:]) - 700) < 10
    cancel = Resampler(rate, 2).process(np.stack([left, right], axis=1).reshape(-1).tobytes())
    assert np.abs(cancel).max() <= 1


def test_ws_url():
    assert captions_ws_url("http://192.168.0.38:8765") == "ws://192.168.0.38:8765/v1/captions"
    assert captions_ws_url("https://hub.local/x/") == "wss://hub.local/x/v1/captions"
    with pytest.raises(ValueError):
        captions_ws_url("192.168.0.38:8765")


def test_live_vtt_alignment(tmp_path):
    vtt = LiveVtt(tmp_path / "r.live.vtt", origin=1000.0)
    vtt.add(995.0, 999.0, "before recording")  # dropped
    vtt.add(999.0, 1002.5, "straddles start")
    vtt.add(3661.25, 3663.0, "one hour in --> later")
    text = (tmp_path / "r.live.vtt").read_text(encoding="utf-8")
    assert text.startswith("WEBVTT\n")
    assert "before recording" not in text
    assert "00:00:00.000 --> 00:00:02.500\nstraddles start" in text
    assert "00:44:21.250 --> 00:44:23.000\none hour in -> later" in text


# ---------------------------------------------------------------------------
# Hub session (mock hub)
# ---------------------------------------------------------------------------

class _Audio:
    """AudioCapture stand-in that plays a tone in real time while subscribed."""
    samplerate = 48000
    channels = 2

    def __init__(self, speech=True):
        self.queues = []
        self.speech = speech
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.queues:
            self.queues.remove(q)

    def _feed(self):
        loud, quiet = _tone(300, 0.02), bytes(960 * 4)
        n = 0
        while not self._stop.is_set():
            n += 1
            # 1.0 s of tone, 0.8 s of silence: one VAD sentence per cycle.
            chunk = loud if self.speech and n % 90 < 50 else quiet
            for q in list(self.queues):
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass
            time.sleep(0.02)

    def close(self):
        self._stop.set()


async def _next(q, kind, timeout=8):
    end = time.monotonic() + timeout
    while True:
        msg = await asyncio.wait_for(q.get(), max(0.01, end - time.monotonic()))
        if msg and msg.get("type") == kind:
            return msg


def test_service_streams_pcm_and_broadcasts_captions(tmp_path):
    hub = MockHub(key="secret-key").start()
    audio = _Audio()

    async def run():
        service = CaptionService(audio, hub.url, "secret-key", "ja")
        assert service.available
        a, b = service.subscribe(), service.subscribe()
        assert (await _next(a, "captions_state"))["state"] == "off"
        assert service._task is None  # viewers alone never start the hub link
        assert service.set_on(True) and not service.set_on(True)
        while (await _next(a, "captions_state"))["state"] != "live":
            pass
        service.begin_recording("s1", tmp_path / "rec.live.vtt", time.time())
        cap_a, cap_b = await _next(a, "caption"), await _next(b, "caption")
        assert cap_a["text"] == cap_b["text"] and cap_a["final"] is True and cap_a["lang"] == "ja"
        assert abs(cap_a["at"] / 1000 - time.time()) < 10
        session = hub.caption_sessions[-1]
        assert session["start"] == {"type": "start", "sample_rate": 16000, "language": "ja"}
        assert session["binary_frames"] > 5
        await _next(a, "caption")
        assert service.end_recording("s1") == tmp_path / "rec.live.vtt"
        assert "-->" in (tmp_path / "rec.live.vtt").read_text(encoding="utf-8")
        service.unsubscribe(a)
        service.unsubscribe(b)
        await asyncio.sleep(0.3)
        assert not service._task.done()  # no viewer left: CC (and the hub link) stays on
        assert service.viewers_idle() > 0
        service.set_on(False)
        for _ in range(100):
            if service._task is None or service._task.done():
                break
            await asyncio.sleep(0.05)
        assert service._task.done() and service.state_message()["state"] == "off"
        assert session["stopped"] is True
        assert audio.queues == []
        # While off a new viewer gets the state only; once on again, a replay.
        c = service.subscribe()
        assert (await _next(c, "captions_state"))["on"] is False and c.empty()
        service.set_on(True)
        d = service.subscribe()
        replay = await _next(d, "caption")
        assert replay.get("replay") is True
        await service.close()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()


def test_service_reports_offline_and_reconnects():
    port = _free_port()
    audio = _Audio()

    async def run():
        service = CaptionService(audio, f"http://127.0.0.1:{port}", None)
        q = service.subscribe()
        service.set_on(True)
        states = []
        while "offline" not in states:
            states.append((await _next(q, "captions_state"))["state"])
        assert "hub unreachable" in service.state_message()["detail"]
        hub = MockHub(port=port).start()
        try:
            while (await _next(q, "captions_state", timeout=10))["state"] != "live":
                pass
            assert (await _next(q, "caption"))["text"]
        finally:
            await service.close()
            hub.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()


def test_service_bad_key_is_reported_without_leaking_it():
    hub = MockHub(key="right").start()
    audio = _Audio()

    async def run():
        service = CaptionService(audio, hub.url, "wrong-secret")
        q = service.subscribe()
        service.set_on(True)
        while True:
            msg = await _next(q, "captions_state")
            if msg["state"] == "offline":
                break
        assert "401" in msg["detail"] and "wrong-secret" not in msg["detail"]
        await service.close()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()


def test_unavailable_without_audio_or_when_disabled():
    service = CaptionService(None, "http://127.0.0.1:1")
    assert not service.available and "audio" in service.reason
    with pytest.raises(ValueError):
        service.subscribe()
    with pytest.raises(ValueError):
        service.set_on(True)
    assert not CaptionService(_FakeAudioStatic(), "http://127.0.0.1:1", enabled=False).available


class _FakeAudioStatic:
    samplerate, channels = 48000, 2


# ---------------------------------------------------------------------------
# Viewer wiring: hello, captions on/off, caption fan-out over /ws
# ---------------------------------------------------------------------------

async def _read(ws, kind, timeout=8):
    end = time.monotonic() + timeout
    while True:
        raw = await asyncio.wait_for(ws.recv(), max(0.01, end - time.monotonic()))
        if isinstance(raw, str):
            msg = json.loads(raw)
            if msg["type"] == kind:
                return msg


def test_viewer_captions_protocol(tmp_path):
    hub = MockHub(caption_every=0.5).start()
    audio = _Audio(speech=False)

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        server = WebViewerServer(_DummyHardware(), config, audio=audio)
        server._cap_label = "dummy"
        await server.start()
        try:
            url = f"ws://127.0.0.1:{config.web_port}/ws"
            async with websockets.connect(url) as one, websockets.connect(url) as two:
                hello = await _read(one, "hello")
                assert hello["captions"] == {"available": True, "reason": "", "on": False, "since": None}
                assert hello["minutes"]["enabled"] is True and hello["minutes"]["default"] == "agy"
                await _read(two, "hello")
                await one.send(json.dumps({"type": "captions", "on": True}))  # one switch for everyone
                caption = await _read(one, "caption")
                assert caption["text"].startswith("テスト字幕")
                assert (await _read(two, "caption"))["text"]
                assert len(hub.caption_sessions) == 1  # one hub link shared by both viewers
                await two.send(json.dumps({"type": "captions", "on": False}))
                while (await _read(one, "captions_state"))["on"]:
                    pass
            for _ in range(60):
                if not audio.queues:
                    break
                await asyncio.sleep(0.05)
            assert audio.queues == []  # hub link closed once CC was switched off
        finally:
            await server.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()


def test_viewer_reports_captions_unavailable_without_audio(tmp_path):
    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        server = WebViewerServer(_DummyHardware(), config)
        server._cap_label = "dummy"
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{config.web_port}/ws") as ws:
                hello = await _read(ws, "hello")
                assert hello["captions"]["available"] is False
                await ws.send(json.dumps({"type": "captions", "on": True}))
                assert (await _read(ws, "captions_state"))["state"] == "unavailable"
        finally:
            await server.stop()

    asyncio.run(run())


def test_block_size_is_100ms():
    assert BLOCK_SAMPLES == 1600
