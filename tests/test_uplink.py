"""Browser microphone uplink: frame parsing, device choice, buffering, ownership."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import websockets

from serial_hid_kvm import _uplink
from serial_hid_kvm._uplink import JitterBuffer, UplinkPlayer, find_output_device, parse_frame
from tests.test_web_auth import _make_server


def frame(rate, samples, tag=0x12):
    return bytes([tag]) + rate.to_bytes(4, "big") + np.asarray(samples, dtype="<i2").tobytes()


# ---- protocol --------------------------------------------------------------

def test_parse_frame_accepts_tagged_mono_pcm():
    rate, pcm = parse_frame(frame(48000, [1, -2, 3]))
    assert rate == 48000
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [1, -2, 3]
    assert parse_frame(bytearray(frame(44100, [0])))[0] == 44100


def test_parse_frame_rejects_malformed_frames():
    assert parse_frame(frame(48000, [1], tag=0x10)) is None        # other tag
    assert parse_frame(bytes([0x12, 0, 0, 0xBB, 0x80])) is None     # header only
    assert parse_frame(frame(48000, [1])[:-1]) is None              # odd PCM length
    assert parse_frame(frame(4000, [1])) is None                    # rate too low
    assert parse_frame(frame(400000, [1])) is None                  # rate too high
    assert parse_frame(frame(48000, np.zeros(48001))) is None       # oversized
    assert parse_frame("not binary") is None
    assert parse_frame(b"") is None


# ---- device choice ---------------------------------------------------------

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
DEVICES = [
    {"name": "ヘッドホン (High Definition Audio", "hostapi": 0,
     "max_input_channels": 0, "max_output_channels": 2},
    {"name": "マイク (High Definition Audio Device)", "hostapi": 2,
     "max_input_channels": 2, "max_output_channels": 0},
    {"name": "スピーカー (Realtek(R) Audio)", "hostapi": 2,
     "max_input_channels": 0, "max_output_channels": 2},
    {"name": "ヘッドホン (High Definition Audio Device)", "hostapi": 2,
     "max_input_channels": 0, "max_output_channels": 2},
    {"name": "EarPods", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
]


def test_find_output_device_prefers_wasapi_substring_match():
    # The MME entry comes first but has a truncated name; WASAPI wins.
    assert find_output_device("ヘッドホン (High Definition Audio Device)", DEVICES, HOSTAPIS) == 3
    assert find_output_device("ヘッドホン", DEVICES, HOSTAPIS) == 3
    assert find_output_device("realtek", DEVICES, HOSTAPIS) == 2  # case-insensitive


def test_find_output_device_falls_back_to_other_hostapis():
    only_mme = [dict(DEVICES[0]), dict(DEVICES[4])]
    assert find_output_device("ヘッドホン", only_mme, HOSTAPIS) == 0


def test_find_output_device_skips_inputs_and_misses():
    assert find_output_device("マイク", DEVICES, HOSTAPIS) is None   # input only
    assert find_output_device("EarPods", DEVICES, HOSTAPIS) is None
    assert find_output_device("nothing like it", DEVICES, HOSTAPIS) is None
    assert find_output_device("", DEVICES, HOSTAPIS) is None
    assert find_output_device("   ", DEVICES, HOSTAPIS) is None


def test_find_output_device_by_index():
    assert find_output_device("2", DEVICES, HOSTAPIS) == 2
    assert find_output_device("1", DEVICES, HOSTAPIS) is None       # input-only index
    assert find_output_device("99", DEVICES, HOSTAPIS) is None


# ---- buffering / resampling ------------------------------------------------

def test_jitter_buffer_prebuffers_and_restarts_after_underrun():
    buf = JitterBuffer(prebuffer=4, limit=100)
    buf.push(np.array([1, 2, 3], dtype=np.int16))
    assert buf.read(2).tolist() == [0, 0]          # still prebuffering
    buf.push(np.array([4, 5], dtype=np.int16))
    assert buf.read(2).tolist() == [1, 2]
    assert buf.read(4).tolist() == [3, 4, 5, 0]    # underrun pads silence
    assert buf.underruns == 1
    buf.push(np.array([6, 7], dtype=np.int16))
    assert buf.read(2).tolist() == [0, 0]          # prebuffering again


def test_jitter_buffer_caps_latency_by_dropping_oldest():
    buf = JitterBuffer(prebuffer=2, limit=5)
    buf.push(np.arange(1, 4, dtype=np.int16))
    buf.push(np.arange(4, 9, dtype=np.int16))
    assert len(buf) == 5 and buf.dropped == 3
    assert buf.read(5).tolist() == [4, 5, 6, 7, 8]


def _fed_player(device_rate=48000):
    player = UplinkPlayer("x")
    player.samplerate = device_rate
    player._stream = object()  # pretend open; feed never touches it
    player._buffer = JitterBuffer(1, 10 * device_rate)
    return player


def test_feed_resamples_to_the_device_rate():
    player = _fed_player()
    for _ in range(50):  # 1 s of 20 ms frames at 44.1 kHz
        player.feed(44100, np.full(882, 1000, dtype="<i2").tobytes())
    assert abs(len(player._buffer) - 48000) <= 2
    out = player._buffer.read(len(player._buffer))
    assert abs(int(np.median(out)) - 1000) <= 2


def test_feed_passes_matching_rate_through_and_ignores_when_closed():
    player = _fed_player()
    player.feed(48000, np.array([5, 6, 7], dtype="<i2").tobytes())
    assert player._buffer.read(3).tolist() == [5, 6, 7]
    player._stream = None
    player.feed(48000, np.array([5], dtype="<i2").tobytes())
    assert len(player._buffer) == 0


def test_player_device_thread_has_com_initialised():
    import os
    if os.name != "nt":
        return
    import ctypes
    player = UplinkPlayer("x")
    # S_FALSE (1): COM was already initialised on this thread by the executor.
    assert player.executor.submit(ctypes.windll.ole32.CoInitializeEx, None, 0).result() == 1


def test_callback_fills_every_channel():
    player = _fed_player()
    player.feed(48000, np.array([9, 8], dtype="<i2").tobytes())
    out = np.ones((3, 2), dtype=np.int16)
    player._callback(out, 3, None, None)
    assert out.tolist() == [[9, 9], [8, 8], [0, 0]]


# ---- server ownership ------------------------------------------------------

class _FakePlayer:
    device_name = "ヘッドホン"
    device_label = "ヘッドホン (High Definition Audio Device)"

    def __init__(self, fail=None):
        self.fail = fail
        self.opened = self.closed = 0
        self.frames = []
        self.executor = ThreadPoolExecutor(1)
        self.threads = set()

    def open(self):
        self.threads.add(threading.get_ident())
        if self.fail:
            raise OSError(self.fail)
        self.opened += 1

    def close(self):
        self.threads.add(threading.get_ident())
        self.closed += 1

    def feed(self, rate, pcm):
        self.frames.append((rate, pcm))


async def _recv_type(ws, kind, timeout=5.0):
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(msg, str):
            data = json.loads(msg)
            if data.get("type") == kind:
                return data


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)


def test_hello_reports_uplink_off_without_output_device():
    async def run():
        server, url = _make_server()
        assert server._uplink is None
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                hello = await _recv_type(ws, "hello")
                assert hello["uplink"] == {"available": False, "device": "",
                                           "on": False, "busy": False}
                await ws.send(json.dumps({"type": "uplink", "on": True}))
                reply = await _recv_type(ws, "uplink_state")
                assert reply["error"] == "unavailable" and not reply["on"]
        finally:
            await server.stop()
    asyncio.run(run())


def test_config_creates_player_for_output_name():
    server, _url = _make_server(uplink_output=" ヘッドホン (High Definition Audio Device) ")
    assert isinstance(server._uplink, UplinkPlayer)
    assert server._uplink.device_name == "ヘッドホン (High Definition Audio Device)"


def test_one_sender_at_a_time_and_release_on_disconnect():
    async def run():
        server, url = _make_server()
        player = server._uplink = _FakePlayer()
        await server.start()
        try:
            async with websockets.connect(url) as a, websockets.connect(url) as b:
                assert (await _recv_type(a, "hello"))["uplink"]["available"]
                await _recv_type(b, "hello")

                await a.send(json.dumps({"type": "uplink", "on": True}))
                state_a = await _recv_type(a, "uplink_state")
                assert state_a["on"] and not state_a["busy"]
                assert state_a["device"] == player.device_label
                state_b = await _recv_type(b, "uplink_state")
                assert state_b["busy"] and not state_b["on"]

                # The latecomer is refused and its frames are never played.
                await b.send(json.dumps({"type": "uplink", "on": True}))
                refused = await _recv_type(b, "uplink_state")
                assert refused["error"] == "busy" and refused["busy"]
                await b.send(frame(48000, [7, 7]))
                await a.send(frame(48000, [1, 2]))
                await a.send(frame(48000, [3])[:-1])      # malformed: dropped
                await _until(lambda: player.frames)
                await asyncio.sleep(0.05)
                assert player.frames == [(48000, np.array([1, 2], "<i2").tobytes())]
                assert player.opened == 1

                await a.close()
                freed = await _recv_type(b, "uplink_state")
                assert not freed["busy"] and not freed["on"]
                assert player.closed == 1 and server._uplink_owner is None

                await b.send(json.dumps({"type": "uplink", "on": True}))
                assert (await _recv_type(b, "uplink_state"))["on"]
                await b.send(json.dumps({"type": "uplink", "on": False}))
                assert not (await _recv_type(b, "uplink_state"))["on"]
                assert player.opened == 2 and player.closed == 2
                # WASAPI: reopening on another thread than the close fails.
                assert len(player.threads) == 1
        finally:
            await server.stop()
    asyncio.run(run())


def test_output_device_failure_is_reported_and_leaves_uplink_free():
    async def run():
        server, url = _make_server()
        server._uplink = _FakePlayer(fail="no output device named like 'x'")
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                await _recv_type(ws, "hello")
                await ws.send(json.dumps({"type": "uplink", "on": True}))
                reply = await _recv_type(ws, "uplink_state")
                assert "no output device" in reply["error"]
                assert not reply["on"] and server._uplink_owner is None
        finally:
            await server.stop()
    asyncio.run(run())


def test_module_constants_match_the_viewer_frame_layout():
    from serial_hid_kvm._web_viewer import _VIEWER_HTML
    assert _uplink.TAG == 0x12 and _uplink.HEADER == 5
    assert "pkt[0] = 0x12;" in _VIEWER_HTML
    assert "setUint32(1, rate)" in _VIEWER_HTML
