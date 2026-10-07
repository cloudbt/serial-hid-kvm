"""Multiple identical capture devices must resolve by identity, not PnP order."""

import unittest
from unittest.mock import Mock, patch

import cv2

from serial_hid_kvm.capture import ScreenCapture, detect_capture_device, _parse_vidpid


class CaptureBindingTests(unittest.TestCase):
    def test_duplicate_capture_names_require_explicit_selection(self):
        devices = [{"device": "0", "name": "USB3 Video"},
                   {"device": "1", "name": "USB3 Video"}]
        with patch("serial_hid_kvm.capture.list_capture_devices", return_value=devices):
            with self.assertRaisesRegex(RuntimeError, "Multiple HDMI"):
                detect_capture_device()

    def test_mf_identity_tracks_index_change(self):
        cap = ScreenCapture("mf:usb#second", width=1920, height=1080)
        device = Mock()
        device.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1080,
            cv2.CAP_PROP_FOURCC: cv2.VideoWriter.fourcc(*"MJPG"),
        }[prop]
        device.getBackendName.return_value = "MSMF"
        enumeration = [{"device": "0", "device_id": "usb#second"},
                       {"device": "1", "device_id": "usb#first"}]
        with patch("serial_hid_kvm.capture.platform.system", return_value="Windows"):
            with patch("serial_hid_kvm.capture.list_capture_devices", return_value=enumeration):
                with patch("serial_hid_kvm.capture.cv2.VideoCapture", return_value=device) as opened:
                    cap._open_device()
        opened.assert_called_once_with(0, cv2.CAP_MSMF)
        self.assertEqual(cap._resolved_device, 0)
        cap.close()

    def test_missing_identity_does_not_open_another_device(self):
        cap = ScreenCapture("mf:missing")
        with patch("serial_hid_kvm.capture.platform.system", return_value="Windows"):
            with patch("serial_hid_kvm.capture.list_capture_devices", return_value=[]):
                with patch("serial_hid_kvm.capture.cv2.VideoCapture") as opened:
                    with self.assertRaisesRegex(RuntimeError, "not uniquely connected"):
                        cap._open_device()
        opened.assert_not_called()

    def test_identity_binding_never_falls_back_to_another_backend_index(self):
        cap = ScreenCapture("mf:usb#first")
        device = Mock()
        device.isOpened.return_value = False
        with patch("serial_hid_kvm.capture.platform.system", return_value="Windows"):
            with patch("serial_hid_kvm.capture.list_capture_devices", return_value=[{"device": "1", "device_id": "usb#first"}]):
                with patch("serial_hid_kvm.capture.cv2.VideoCapture", return_value=device) as opened:
                    with self.assertRaisesRegex(RuntimeError, "Failed to open"):
                        cap._open_device()
        opened.assert_called_once_with(1, cv2.CAP_MSMF)
        cap.close()

    def test_symbolic_links_have_case_insensitive_vidpid(self):
        self.assertEqual(_parse_vidpid(r"\\?\usb#vid_345f&pid_2131#device"), "345F:2131")


if __name__ == "__main__":
    unittest.main()
