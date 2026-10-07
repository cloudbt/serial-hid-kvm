"""Ensure slow capture startup cannot block WebSocket negotiation/status."""

import asyncio
import json
import threading
import unittest

import websockets

from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config
from tests.test_web_auth import _DummyCapture, _DummyHardware, _free_port


class WebCaptureStatusTests(unittest.TestCase):
    def test_hello_arrives_before_slow_open_and_status_updates_afterward(self):
        async def run():
            release_open = threading.Event()

            class SlowCapture(_DummyCapture):
                ready = False

                def start_capture_thread(self):
                    release_open.wait(3)
                    self.ready = True

                def get_cached_info(self):
                    return {"status": "ready" if self.ready else "pending",
                            "width": 1920 if self.ready else None,
                            "height": 1080 if self.ready else None,
                            "requested_width": 1920, "requested_height": 1280,
                            "resolution_matches_request": False if self.ready else None}

            config = Config()
            config.web_host = "127.0.0.1"
            config.web_port = _free_port()
            config.capture_width, config.capture_height = 1920, 1280
            hardware = _DummyHardware()
            hardware._cap = SlowCapture()
            server = WebViewerServer(hardware, config)
            server._cap_label = "dummy"
            await server.start()
            try:
                async with websockets.connect(f"ws://127.0.0.1:{config.web_port}/ws") as ws:
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 1))
                    self.assertEqual(hello["type"], "hello")
                    self.assertEqual(hello["capture_status"]["status"], "pending")
                    self.assertEqual(hello["capture_request"], {"width": 1920, "height": 1280})
                    release_open.set()
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), 1)
                        if isinstance(msg, str):
                            status = json.loads(msg)
                            if (status["type"] == "capture_status"
                                    and status["info"]["status"] == "ready"):
                                self.assertEqual(status["info"]["height"], 1080)
                                self.assertFalse(status["info"]["resolution_matches_request"])
                                break
            finally:
                release_open.set()
                await server.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
