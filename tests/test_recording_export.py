"""Real media export and WebSocket recording lifecycle regression tests."""
import asyncio
import json
import io
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest
import websockets

from serial_hid_kvm._recording import convert_recording, _timeline
from tests.test_web_auth import _make_server


@pytest.mark.parametrize("audio", [True, False])
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_export_preserves_video_and_audio(tmp_path, audio):
    source = tmp_path / "recording with spaces.webm"
    args = [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=25:duration=2"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                 "-c:a", "libopus"]
    args += ["-c:v", "libvpx-vp9", str(source)]
    subprocess.run(args, check=True, capture_output=True)
    original = source.read_bytes()
    result = convert_recording(source)
    info = json.loads(subprocess.check_output([
        shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(result)]))
    assert source.read_bytes() == original
    assert sum(s["codec_type"] == "audio" for s in info["streams"]) == int(audio)
    assert info["streams"][0]["codec_name"] == "h264"
    assert source.with_suffix(".conversion.log").read_text().startswith("OK:")
    # Existing exports are immutable, even when conversion is requested again.
    mp4_bytes = result.read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        convert_recording(source)
    assert result.read_bytes() == mp4_bytes
    assert not list(tmp_path.glob("*.partial.mp4"))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_browser_h264_webm_keeps_video_packets_and_converts_audio(tmp_path):
    source = tmp_path / "browser-h264.webm"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=25:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=2",
                    # Chrome's "webm;codecs=h264" is Matroska; ffmpeg's webm muxer refuses H.264.
                    "-c:v", "libx264", "-c:a", "libopus", "-f", "matroska", str(source)],
                   check=True, capture_output=True)
    result = convert_recording(source)
    info = json.loads(subprocess.check_output([
        shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(result)]))
    codecs = sorted((s["codec_type"], s["codec_name"]) for s in info["streams"])
    assert codecs == [("audio", "aac"), ("video", "h264")]
    assert _timeline(shutil.which("ffprobe"), result)[0] == _timeline(shutil.which("ffprobe"), source)[0]


def test_missing_tools_keep_source(tmp_path):
    source = tmp_path / "recording.webm"
    source.write_bytes(b"original")
    with patch("serial_hid_kvm._recording.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="unavailable"):
            convert_recording(source)
    assert source.read_bytes() == b"original"


def test_reordered_video_packet_intervals_do_not_extend_past_last_frame(tmp_path):
    class Probe:
        # Presentation timestamps differ from decode order; a capture gap is
        # reflected in an earlier packet interval, not extra content at the end.
        stdout = io.StringIO("0.000,0.033\n3.000,0.660\n3.164,0.033\n3.131,0.001\n")
        def wait(self, timeout): return 0
        def poll(self): return 0
    with patch("serial_hid_kvm._recording.subprocess.Popen", return_value=Probe()):
        count, duration = _timeline("ffprobe", tmp_path / "capture.mp4")
    assert count == 4
    assert duration == pytest.approx(3.197)


@pytest.mark.parametrize("disconnect", [False, True])
def test_server_exports_only_after_all_chunks_are_closed(tmp_path, disconnect):
    async def run():
        server, url = _make_server(recording_dir=str(tmp_path))
        received = []
        def fake_export(path):
            path = Path(path)
            # Reopening exclusively catches leaked handles on Windows.
            received.append(path.read_bytes())
            return path.with_suffix(".mp4")
        with patch("serial_hid_kvm._web_viewer.convert_recording", side_effect=fake_export):
            await server.start()
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps({"type":"rec_start", "filename":"session.webm"}))
                    for chunk in (b"first", b"second", b"final"):
                        await ws.send(b"\x10" + chunk)
                    if not disconnect:
                        await ws.send(json.dumps({"type":"rec_stop"}))
                        while True:
                            msg = await asyncio.wait_for(ws.recv(), 5)
                            if isinstance(msg, str) and json.loads(msg)["type"] == "rec_exported":
                                break
            finally:
                await server.stop()
        assert received == [b"firstsecondfinal"]
    asyncio.run(run())


def test_filename_collision_does_not_truncate(tmp_path):
    server, _ = _make_server(recording_dir=str(tmp_path))
    file, path = server._open_recording("same.webm")
    file.write(b"keep me")
    file.close()
    with pytest.raises(FileExistsError):
        server._open_recording("same.webm")
    assert path.read_bytes() == b"keep me"
