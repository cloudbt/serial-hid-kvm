"""Tests for the web viewer's LAN/WAN split (adaptive stream, audio gating).

Runs a real WebViewerServer with dummy hardware; WAN classification is
forced per-test by overriding ``_client_is_wan``.
"""

import asyncio
import json
import queue

import websockets

from serial_hid_kvm._web_viewer import (
    _WAN_CREDITS,
    WebViewerServer,
    _is_private_address,
)
from serial_hid_kvm.config import Config
from tests.test_web_auth import _DummyHardware, _free_port


class _FrameCapture:
    """Dummy capture that always has a (fake) JPEG frame available."""

    def start_capture_thread(self):
        pass

    def close(self):
        pass

    def get_frame_jpeg(self, quality=85, force_reencode=False):
        return (b"\xff\xd8fake-jpeg", 640, 480)


class _FakeAudio:
    samplerate = 48000
    channels = 2

    def __init__(self):
        self.queues: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        self.queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        if q in self.queues:
            self.queues.remove(q)

    def feed(self, data: bytes):
        for q in self.queues:
            q.put(data)


def _make_server(*, wan: bool, audio=None, frames=False):
    config = Config()
    config.web_host = "127.0.0.1"
    config.web_port = _free_port()
    hardware = _DummyHardware()
    if frames:
        hardware._cap = _FrameCapture()
    server = WebViewerServer(hardware, config, audio=audio)
    server._cap_label = "dummy"
    if wan:
        server._client_is_wan = lambda ip: True
    return server, f"ws://127.0.0.1:{config.web_port}/ws"


async def _read_until(ws, msg_type: str) -> dict:
    """Consume messages until one of *msg_type* arrives; return it."""
    while True:
        msg = await asyncio.wait_for(ws.recv(), 5)
        if isinstance(msg, str):
            data = json.loads(msg)
            if data["type"] == msg_type:
                return data


async def _read_until_hello(ws) -> dict:
    return await _read_until(ws, "hello")


async def _collect_binary(ws, tag: int, duration: float) -> int:
    """Count binary messages with the given tag byte for *duration* secs."""
    loop = asyncio.get_running_loop()
    count = 0
    end = loop.time() + duration
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            return count
        try:
            msg = await asyncio.wait_for(ws.recv(), remaining)
        except asyncio.TimeoutError:
            return count
        if isinstance(msg, (bytes, bytearray)) and msg and msg[0] == tag:
            count += 1


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_is_private_address():
    for ip in ("127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "172.31.255.1",
               "192.168.0.197", "169.254.1.1", "fe80::1", "fd00::5"):
        assert _is_private_address(ip), ip
    for ip in ("125.103.192.173", "8.8.8.8", "2001:4860:4860::8888",
               "not-an-ip", ""):
        assert not _is_private_address(ip), ip


def test_webrtc_params():
    config = Config()  # webrtc_fps=60, webrtc_bitrate=16 Mbps

    # LAN defaults: full rate, floor at half the cap (original behaviour).
    assert WebViewerServer._webrtc_params(config, False, {}) == \
        (60, 16_000_000, 16_000_000, 8_000_000)

    # WAN defaults: 30 fps, soft 4 Mbps start, no floor.
    assert WebViewerServer._webrtc_params(config, True, {}) == \
        (30, 16_000_000, 4_000_000, None)

    # Explicit preset: cap and fps honoured, still no WAN floor.
    assert WebViewerServer._webrtc_params(
        config, True, {"bitrate": 8_000_000, "fps": 30}) == \
        (30, 8_000_000, 4_000_000, None)

    # A preset below the WAN start lowers the start too.
    assert WebViewerServer._webrtc_params(
        config, True, {"bitrate": 2_000_000, "fps": 30}) == \
        (30, 2_000_000, 2_000_000, None)

    # LAN preset keeps the half-cap floor semantics.
    assert WebViewerServer._webrtc_params(
        config, False, {"bitrate": 2_000_000, "fps": 30}) == \
        (30, 2_000_000, 2_000_000, 1_000_000)

    # Requests are clamped to the configured maxima / sane minima.
    fps, cap, _, _ = WebViewerServer._webrtc_params(
        config, True, {"bitrate": 999_999_999, "fps": 240})
    assert cap == 16_000_000 and fps == 60
    fps, _, _, _ = WebViewerServer._webrtc_params(config, True, {"fps": 1})
    assert fps == 5


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

def test_lan_client_streams_without_acks():
    async def run():
        server, url = _make_server(wan=False, frames=True)
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                hello = await _read_until_hello(ws)
                assert not hello.get("adaptive")
                # LAN: fixed-pace stream, no acks needed — frames just flow.
                frames = await _collect_binary(ws, 0x01, 0.5)
                assert frames >= 3
        finally:
            await server.stop()

    asyncio.run(run())


def test_wan_client_is_credit_paced():
    async def run():
        server, url = _make_server(wan=True, frames=True)
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                hello = await _read_until_hello(ws)
                assert hello.get("adaptive") is True
                # Without acks only _WAN_CREDITS frames may be in flight.
                frames = await _collect_binary(ws, 0x01, 1.0)
                assert frames == _WAN_CREDITS
                # Returning the credits releases more frames.
                for _ in range(_WAN_CREDITS):
                    await ws.send(json.dumps({"type": "frame_ack"}))
                frames = await _collect_binary(ws, 0x01, 1.0)
                assert frames >= 1
        finally:
            await server.stop()

    asyncio.run(run())


def test_wan_audio_is_subscribed_on_demand():
    async def run():
        audio = _FakeAudio()
        server, url = _make_server(wan=True, audio=audio)
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                # audio_config announces the off-by-default state.
                msg = await _read_until(ws, "audio_config")
                assert msg["on"] is False
                assert audio.queues == []  # not subscribed server-side

                audio.feed(b"\x01\x02")  # goes nowhere
                assert await _collect_binary(ws, 0x02, 0.5) == 0

                await ws.send(json.dumps({"type": "audio", "on": True}))
                await asyncio.sleep(0.3)
                assert len(audio.queues) == 1
                audio.feed(b"\x01\x02")
                assert await _collect_binary(ws, 0x02, 1.0) >= 1

                await ws.send(json.dumps({"type": "audio", "on": False}))
                await asyncio.sleep(0.3)
                assert audio.queues == []
        finally:
            await server.stop()

    asyncio.run(run())


def test_lan_audio_is_always_on():
    async def run():
        audio = _FakeAudio()
        server, url = _make_server(wan=False, audio=audio)
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                msg = await _read_until(ws, "audio_config")
                assert msg["on"] is True
                await asyncio.sleep(0.1)
                assert len(audio.queues) == 1  # subscribed at connect
                audio.feed(b"\x01\x02")
                assert await _collect_binary(ws, 0x02, 1.0) >= 1
            await asyncio.sleep(0.3)
            assert audio.queues == []  # unsubscribed on disconnect
        finally:
            await server.stop()

    asyncio.run(run())
