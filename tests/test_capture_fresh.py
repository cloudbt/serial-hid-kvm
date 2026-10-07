"""Regression tests for cached API frames and negotiated capture dimensions."""

import base64
import threading
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from serial_hid_kvm.capture import ScreenCapture
from serial_hid_kvm.config import Config
from serial_hid_kvm.server import ApiDispatcher, KvmHardware


class CaptureFreshTests(unittest.TestCase):
    def setUp(self):
        self.cap = ScreenCapture(0, width=1920, height=1280)
        self.cap._latest_frame = np.zeros((12, 18, 3), dtype=np.uint8)
        self.cap._latest_jpeg = b"stale"
        self.cap._frame_seq = 5
        self.addCleanup(self.cap.close)

    def test_api_waits_for_frame_after_request_even_with_existing_cache(self):
        frame = np.full((12, 18, 3), 180, dtype=np.uint8)
        delivered = threading.Event()

        def start():
            self.cap._running = True

            def publish():
                time.sleep(0.03)
                self.cap._latest_frame = frame
                self.cap._frame_seq += 1
                delivered.set()

            worker = threading.Thread(target=publish)
            worker.start()
            self.addCleanup(worker.join)

        hardware = Mock()
        hardware.get_capture.return_value = self.cap
        dispatcher = ApiDispatcher(hardware, Config())
        with patch.object(self.cap, "ensure_streaming", side_effect=start):
            result = dispatcher._do_capture_frame({})
        self.assertTrue(delivered.is_set())
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(result["jpeg_b64"]),
                                            dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(decoded.mean(), 170)
        self.assertEqual((result["width"], result["height"]), (18, 12))

    def test_stalled_capture_raises_instead_of_returning_cached_frame(self):
        with patch.object(self.cap, "ensure_streaming"):
            with self.assertRaisesRegex(TimeoutError, "No new capture frame"):
                self.cap.capture_fresh_jpeg(timeout=0.03)

    def test_snapshot_restarts_capture_after_viewer_releases_device(self):
        self.cap.close()
        device = Mock()

        def read():
            time.sleep(0.005)
            return True, np.full((12, 18, 3), 180, dtype=np.uint8)

        device.read.side_effect = read

        def open_device():
            self.cap._cap = device

        with patch.object(self.cap, "_open_device", side_effect=open_device):
            jpeg, width, height = self.cap.capture_fresh_jpeg()
        self.assertTrue(self.cap.running)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual((width, height), (18, 12))

    def test_close_clears_cached_frames_and_crop(self):
        self.cap._crop_rect = (1, 11, 1, 17)
        self.cap._crop_frame_counter = 20
        self.cap._cap = Mock()
        self.cap.close()
        self.assertIsNone(self.cap.get_frame_jpeg())
        self.assertIsNone(self.cap._latest_frame)
        self.assertIsNone(self.cap._latest_jpeg)
        self.assertIsNone(self.cap._crop_rect)
        self.assertEqual(self.cap._crop_frame_counter, 0)

    def test_close_waits_for_in_flight_snapshot(self):
        frame = np.full((12, 18, 3), 180, dtype=np.uint8)
        close_started = threading.Event()
        close_finished = threading.Event()

        def close():
            close_started.set()
            self.cap.close()
            close_finished.set()

        def start():
            worker = threading.Thread(target=close)
            worker.start()
            self.addCleanup(worker.join)
            self.assertTrue(close_started.wait(1))
            self.assertFalse(close_finished.is_set())
            self.cap._latest_frame = frame
            self.cap._frame_seq += 1

        with patch.object(self.cap, "ensure_streaming", side_effect=start):
            jpeg, width, height = self.cap.capture_fresh_jpeg()
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual((width, height), (18, 12))
        self.assertTrue(close_finished.wait(1))
        self.assertIsNone(self.cap.get_frame_jpeg())

    def test_device_info_exposes_request_and_actual_separately(self):
        device = Mock()
        device.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FRAME_WIDTH: 1920,
            cv2.CAP_PROP_FRAME_HEIGHT: 1080,
            cv2.CAP_PROP_FOURCC: cv2.VideoWriter.fourcc(*"MJPG"),
            cv2.CAP_PROP_FPS: 30,
        }[prop]
        device.getBackendName.return_value = "MSMF"
        self.cap._cap = device
        with patch.object(self.cap, "_ensure_open"):
            info = self.cap.get_info()
        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertEqual((info["requested_width"], info["requested_height"]),
                         (1920, 1280))
        self.assertFalse(info["resolution_matches_request"])

    def test_open_warns_when_driver_rejects_requested_resolution(self):
        device = Mock()
        device.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FRAME_WIDTH: 1920,
            cv2.CAP_PROP_FRAME_HEIGHT: 1080,
            cv2.CAP_PROP_FOURCC: cv2.VideoWriter.fourcc(*"MJPG"),
        }[prop]
        device.getBackendName.return_value = "MSMF"
        with patch("serial_hid_kvm.capture.cv2.VideoCapture", return_value=device):
            with self.assertLogs("serial_hid_kvm.capture", level="WARNING") as logs:
                self.cap._open_device()
        self.assertIn("requested 1920x1280, actual 1920x1080", logs.output[0])
        status = self.cap.get_cached_info()
        self.assertEqual(status["status"], "ready")
        self.assertFalse(status["resolution_matches_request"])
        # The device is already 1920 wide: only the differing height is set.
        set_properties = [call.args[0] for call in device.set.call_args_list]
        self.assertNotIn(cv2.CAP_PROP_FRAME_WIDTH, set_properties)
        self.assertIn(cv2.CAP_PROP_FRAME_HEIGHT, set_properties)

    def test_status_request_does_not_open_video_or_serial_hardware(self):
        hardware = KvmHardware(Config())
        dispatcher = ApiDispatcher(hardware, Config())
        with patch("serial_hid_kvm.capture.cv2.VideoCapture") as video:
            with patch.object(hardware, "get_ch9329") as serial:
                status = dispatcher._do_get_capture_status({})
        video.assert_not_called()
        serial.assert_not_called()
        self.assertEqual(status["status"], "pending")
        self.assertIsNone(status["width"])
        self.assertIsNone(status["resolution_matches_request"])

    def test_close_invalidates_negotiated_status(self):
        self.cap._negotiated_info = {"width": 1920, "height": 1080}
        self.assertEqual(self.cap.get_cached_info()["status"], "ready")
        self.cap.close()
        self.assertEqual(self.cap.get_cached_info()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
