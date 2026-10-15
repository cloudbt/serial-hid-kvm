import asyncio
import json
import struct
import shutil
import subprocess
from unittest.mock import patch

import pytest
import websockets

from serial_hid_kvm._recording_session import RecordingSessions
from tests.test_web_auth import _make_server


ID = "a" * 32
NAME = "recording-2026-10-15T00-00-00.webm"


def test_reconnect_restart_and_lost_ack_are_exactly_once(tmp_path):
    store = RecordingSessions(tmp_path)
    assert store.attach(ID, NAME)["next"] == 0
    store.write(ID, 0, b"header")
    # A new process and a new WebSocket can safely replay an unacknowledged block.
    store = RecordingSessions(tmp_path)
    assert store.attach(ID, NAME)["next"] == 1
    assert store.write(ID, 0, b"header")["next"] == 1
    store.write(ID, 1, b"tail")
    source, output = store.finish(ID, 2)
    assert source.read_bytes() == b"headertail"
    assert source.parent.parent.name == ".recording-sessions"
    assert output.parent == tmp_path
    assert len(list(tmp_path.glob("*.webm"))) == 0
    assert store.finish(ID, 2) == (source, output)  # Repeated manual finish is safe.
    with pytest.raises(ValueError, match="finished"):
        store.write(ID, 2, b"extra")


def test_uncommitted_tail_is_rolled_back(tmp_path):
    store = RecordingSessions(tmp_path)
    store.attach(ID, NAME)
    store.write(ID, 0, b"good")
    source = store._paths(ID)[1]
    with source.open("ab") as file:
        file.write(b"uncommitted")
    assert RecordingSessions(tmp_path).attach(ID, NAME)["next"] == 1
    assert source.read_bytes() == b"good"


def test_invalid_paths_order_and_early_finish_are_rejected(tmp_path):
    store = RecordingSessions(tmp_path)
    with pytest.raises(ValueError):
        store.attach("../outside", NAME)
    with pytest.raises(ValueError):
        store.attach(ID, "../outside.webm")
    store.attach(ID, NAME)
    with pytest.raises(ValueError, match="Out-of-order"):
        store.write(ID, 1, b"tail")
    with pytest.raises(ValueError, match="unacknowledged"):
        store.finish(ID, 1)
    with pytest.raises(ValueError, match="size"):
        store.write(ID, 0, b"x" * (store.MAX_CHUNK + 1))


def test_one_export_after_multiple_viewer_disconnects(tmp_path):
    async def run():
        server, url = _make_server(recording_dir=str(tmp_path))
        exports = []
        def export(source, destination):
            exports.append(source.read_bytes())
            destination.write_bytes(b"verified mp4")
            return destination
        async def receive(ws, kind):
            while True:
                msg = await asyncio.wait_for(ws.recv(), 5)
                if isinstance(msg, str) and json.loads(msg)["type"] == kind:
                    return json.loads(msg)
        with patch("serial_hid_kvm._web_viewer.convert_recording", side_effect=export):
            await server.start()
            try:
                for index in range(3):
                    async with websockets.connect(url) as ws:
                        await ws.send(json.dumps({"type":"rec_session_start", "session":ID, "filename":NAME}))
                        assert (await receive(ws, "rec_session_ready"))["next"] == index
                        if index:
                            # Replay last block because its ACK was lost at disconnect.
                            await ws.send(b"\x11" + struct.pack(">I", index-1) + bytes([index-1]))
                            assert (await receive(ws, "rec_ack"))["next"] == index
                        await ws.send(b"\x11" + struct.pack(">I", index) + bytes([index]))
                        assert (await receive(ws, "rec_ack"))["next"] == index+1
                    assert exports == [], "Disconnect must never export or end the session"
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps({"type":"rec_session_start", "session":ID, "filename":NAME}))
                    await receive(ws, "rec_session_ready")
                    await ws.send(json.dumps({"type":"rec_session_finish", "session":ID, "next":3}))
                    await receive(ws, "rec_session_saved")
                    await receive(ws, "rec_exported")
            finally:
                await server.stop()
        assert exports == [b"\x00\x01\x02"]
        assert len(list(tmp_path.glob("*.mp4"))) == 1
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_audio_video_survives_reconnect_as_single_export(tmp_path):
    original = tmp_path / "fixture.webm"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=25:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libvpx-vp9", "-c:a", "libopus", str(original)],
                   check=True, capture_output=True)
    data = original.read_bytes()
    output = tmp_path / "exports"
    async def run():
        server, url = _make_server(recording_dir=str(output))
        async def receive(ws, kind):
            while True:
                msg = await asyncio.wait_for(ws.recv(), 15)
                if isinstance(msg, str) and json.loads(msg)["type"] == kind:
                    return json.loads(msg)
        await server.start()
        try:
            block_size = (len(data) + 2) // 3
            chunks = [data[i:i+block_size] for i in range(0, len(data), block_size)]
            for index, block in enumerate(chunks):
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps({"type":"rec_session_start", "session":ID, "filename":NAME}))
                    await receive(ws, "rec_session_ready")
                    await ws.send(b"\x11" + struct.pack(">I", index) + block)
                    await receive(ws, "rec_ack")
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type":"rec_session_start", "session":ID, "filename":NAME}))
                await receive(ws, "rec_session_ready")
                await ws.send(json.dumps({"type":"rec_session_finish", "session":ID, "next":len(chunks)}))
                exported = await receive(ws, "rec_exported")
        finally:
            await server.stop()
        assert len(list(output.glob("*.mp4"))) == 1
        source = output / ".recording-sessions" / ID / "source.webm"
        assert source.read_bytes() == data
        info = json.loads(subprocess.check_output([shutil.which("ffprobe"), "-v", "error",
            "-show_streams", "-of", "json", exported["path"]]))
        assert [s["codec_name"] for s in info["streams"]] == ["h264", "aac"]
    asyncio.run(run())
