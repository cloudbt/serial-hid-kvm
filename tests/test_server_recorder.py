"""Server-side recording: audio blocks, session files and a real ffmpeg run."""
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from serial_hid_kvm import _server_recorder as srec
from serial_hid_kvm._recording import convert_recording
from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm._recording_session import RecordingSessions


class AudioBlockTests(unittest.TestCase):
    def test_short_input_is_padded_with_silence(self):
        buf = srec.AudioBuffer()
        buf.push(np.full((100, 2), 7, np.int16))
        block = buf.read()
        self.assertEqual(block.shape, (srec.BLOCK, 2))
        self.assertTrue((block[:100] == 7).all())
        self.assertTrue((block[100:] == 0).all())

    def test_backlog_over_300ms_keeps_the_newest_samples(self):
        buf = srec.AudioBuffer()
        for value in range(40):                          # 40 blocks = 800 ms
            buf.push(np.full((srec.BLOCK, 2), value, np.int16))
        first = buf.read()
        self.assertEqual(int(first[0, 0]), 40 - srec.MAX_BACKLOG_BLOCKS)

    def test_mono_16k_becomes_stereo_48k(self):
        out = srec.to_block_format(np.arange(160, dtype=np.int16), 16000, 1)
        self.assertEqual(out.shape, (480, 2))
        self.assertTrue((out[:, 0] == out[:, 1]).all())

    def test_mix_clips_instead_of_wrapping(self):
        loud = np.full((srec.BLOCK, 2), 30000, np.int16)
        self.assertTrue((srec.mix([loud, loud]) == 32767).all())


class ServerSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = RecordingSessions(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_server_session_lists_its_mkv_until_the_mp4_exists(self):
        session = "a" * 32
        state, source = self.sessions.create_server(session, "recording-2026-10-20T11-00-00-000-JST",
                                                    {"minutes": "off", "fps": 15, "audio_source": "mix"})
        self.assertEqual(source.name, "source.mkv")
        self.assertEqual(self.sessions.unfinished_server_sessions(), [session])
        with self.assertRaisesRegex(ValueError, "empty"):
            self.sessions.finish_server(session)
        source.write_bytes(b"x" * 10)
        src, destination = self.sessions.finish_server(session, 12.5, "2026-10-20T02:00:12+00:00")
        manifest = json.loads((source.parent / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["finished"] and manifest["server"])
        self.assertEqual((manifest["minutes"], manifest["duration_seconds"]), ("off", 12.5))
        self.assertEqual(destination.name, "recording-2026-10-20T11-00-00-000-JST-aaaaaaaa.mp4")
        self.assertEqual(self.sessions.unfinished_server_sessions(), [])
        entry = next(iter(RecordingLibrary(self.root).catalog().values()))
        self.assertEqual(entry["path"], src)
        self.assertTrue(entry["name"].endswith(".mkv"))
        self.assertIn("MKV", entry["state"])

    def test_invalid_filename_is_refused(self):
        with self.assertRaisesRegex(ValueError, "filename"):
            self.sessions.create_server("b" * 32, "../evil", {})


class FakeCapture:
    def __init__(self):
        self.seq, self.running = 0, True
        self.frame = np.zeros((360, 640, 3), np.uint8)
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self):
        while self.running:
            self.frame[:, :, 1] = (self.seq * 5) % 255
            self.seq += 1
            time.sleep(1 / 60)

    def ensure_streaming(self):
        pass

    def get_frame_if_newer(self, last):
        return (None, self.seq) if self.seq == last else (self.frame.copy(), self.seq)


class FakeHdmi:
    samplerate, channels = 48000, 2

    def __init__(self):
        self.queues, self.running = [], True
        threading.Thread(target=self._tick, daemon=True).start()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        self.queues.remove(q)

    def _tick(self):
        t = 0
        while self.running:
            tone = (np.sin(np.arange(t, t + 960) * 2 * np.pi * 440 / 48000) * 8000).astype(np.int16)
            for q in list(self.queues):
                q.put_nowait(np.repeat(tone, 2).tobytes())
            t += 960
            time.sleep(0.02)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ServerRecorderRunTests(unittest.TestCase):
    def test_records_constant_rate_video_with_audio_and_remuxes_to_mp4(self):
        capture, hdmi = FakeCapture(), FakeHdmi()
        stopped = []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            recorder = srec.ServerRecorder(capture, hdmi, mic_name="", encoders=("libx264",),
                                           on_stop=stopped.append)
            state = recorder.start("c" * 32, source, fps=15, source="hdmi")
            self.assertTrue(state["active"])
            time.sleep(2.2)
            final = recorder.stop()
            capture.running = hdmi.running = False
            self.assertFalse(final["active"])
            self.assertEqual(final.get("error"), None)
            self.assertEqual(final["stopped_reason"], "manual")
            self.assertEqual(stopped[0]["session"], "c" * 32)
            self.assertGreaterEqual(final["frames"], 30)       # 15 fps x ~2 s
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration",
                 "-of", "json", str(source)], capture_output=True, text=True, check=True).stdout)
            self.assertEqual(sorted((s["codec_type"], s["codec_name"]) for s in probe["streams"]),
                             [("audio", "aac"), ("video", "h264")])
            self.assertAlmostEqual(float(probe["format"]["duration"]), final["frames"] / 15, delta=0.3)
            mp4 = convert_recording(source, Path(tmp) / "out.mp4")
            self.assertTrue(mp4.is_file())

    def test_second_start_is_refused_while_recording(self):
        capture = FakeCapture()
        with tempfile.TemporaryDirectory() as tmp:
            recorder = srec.ServerRecorder(capture, None, mic_name="", encoders=("libx264",))
            recorder.start("d" * 32, Path(tmp) / "a.mkv", fps=15, source="none")
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    recorder.start("e" * 32, Path(tmp) / "b.mkv")
            finally:
                recorder.stop()
                capture.running = False

    def test_max_duration_stops_by_itself(self):
        capture = FakeCapture()
        done = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            recorder = srec.ServerRecorder(capture, None, mic_name="", encoders=("libx264",),
                                           max_seconds=1, on_stop=lambda s: done.set())
            recorder.start("f" * 32, Path(tmp) / "a.mkv", fps=15, source="none")
            self.assertTrue(done.wait(15))
            self.assertEqual(recorder.status()["stopped_reason"], "max_duration")
            capture.running = False


if __name__ == "__main__":
    unittest.main()
