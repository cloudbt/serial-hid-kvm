"""Agent API (JSON-Lines API server): server recording, CC and meeting points."""
import asyncio
import json
import threading

import pytest
import websockets

from serial_hid_kvm.client import KvmClient, KvmClientError
from serial_hid_kvm.config import Config
from serial_hid_kvm.server import ApiDispatcher, TcpServer
from serial_hid_kvm._web_viewer import WebViewerServer
from tests.mock_ai_hub import MockHub
from tests.test_captions import _Audio, _read
from tests.test_live_meeting import _live
from tests.test_web_auth import _DummyHardware, _free_port


class _Recorder:
    """ServerRecorder stand-in: no ffmpeg, stops through the same on_stop hook."""

    def __init__(self, on_stop):
        self.on_stop = on_stop
        self._status = {"active": False}
        self.stopped = threading.Event()

    @property
    def active(self):
        return bool(self._status.get("active"))

    def status(self):
        return dict(self._status)

    def start(self, session, path, fps=15, source="mix", started_epoch=None):
        self._status = {"active": True, "session": session, "fps": fps, "source": source,
                        "encoder": "fake", "warnings": []}
        return self.status()

    def stop(self, reason="manual", timeout=90):
        session = self._status.get("session")
        self._status = {"active": False}
        self.on_stop({"session": session, "stopped_reason": reason, "duration": 1.0})
        return self.status()


def test_agent_api_drives_recording_cc_and_meeting_points(tmp_path):
    hub = MockHub(caption_every=0.3).start()
    audio = _Audio(speech=False)

    async def run():
        config = Config()
        config.web_port = _free_port()
        config.recording_dir = str(tmp_path)
        config.ai_hub_url = hub.url
        web = WebViewerServer(_DummyHardware(), config, audio=audio)
        web._cap_label = "dummy"
        web._server_recorder = _Recorder(web._server_recording_stopped)
        saved = []

        async def save(session, status):
            saved.append((session, status["stopped_reason"]))
            return {"session": session, "saved": True, "path": str(tmp_path / "x.mp4"),
                    "stopped_reason": status["stopped_reason"], "recording_id": "r1"}
        web._save_server_recording = save
        calls = []
        web._meeting.runners = {n: (lambda w, p, t: calls.append(t) or _live()) for n in ("agy", "codex", "mac-qwen")}
        dispatcher = ApiDispatcher(_DummyHardware(), config)
        dispatcher.web = web
        api_port = _free_port()
        tcp = TcpServer(dispatcher, "127.0.0.1", api_port)
        await web.start()
        await tcp.start()
        client = KvmClient("127.0.0.1", api_port)

        def call(method, **params):
            return asyncio.to_thread(client.call, method, params)

        try:
            async with websockets.connect(f"ws://127.0.0.1:{config.web_port}/ws") as viewer:
                await _read(viewer, "hello")
                status = await call("recording_status")
                assert status["recording"]["active"] is False and status["last"] is None
                with pytest.raises(KvmClientError, match="minutes"):
                    await call("recording_start", minutes="gpt")
                started = await call("recording_start", fps=15, source="hdmi")
                assert started["recording"]["active"] and started["recording"]["started_by"] == "api"
                while not (await _read(viewer, "srec_state")).get("active"):
                    pass  # the open viewer sees the API recording
                with pytest.raises(KvmClientError, match="already running"):
                    await call("recording_start")
                await call("recording_stop")
                for _ in range(50):
                    status = await call("recording_status")
                    if status["last"]:
                        break
                    await asyncio.sleep(0.05)
                assert status["last"]["saved"] and status["last"]["path"].endswith("x.mp4")
                assert saved == [(started["session"], "api")]
                manifest = json.loads(next(tmp_path.rglob("session.json")).read_text(encoding="utf-8"))
                assert manifest["minutes"] == "off"  # no AI minutes unless the caller asks
                with pytest.raises(KvmClientError, match="No server recording"):
                    await call("recording_stop")

                with pytest.raises(KvmClientError, match="on: true or false"):
                    await call("captions_set", on="yes")
                cc = await call("captions_set", on=True)
                assert cc["available"] and cc["on"] is True
                while not (await _read(viewer, "captions_state"))["on"]:
                    pass  # every viewer follows the switch
                await _read(viewer, "caption")
                points = await call("meeting_points")
                assert points["session"]["id"] and points["data"] is None and calls == []
                started = await call("meeting_summarize")
                assert started["started"] is True
                for _ in range(100):
                    points = await call("meeting_points")
                    if points["data"]:
                        break
                    await asyncio.sleep(0.05)
                assert points["data"]["current_topic"] == "リリース計画" and len(calls) == 1
                minutes = await call("meeting_minutes")
                assert minutes["minutes"]["kind"] == "captions"
                assert (await call("captions_set", on=False))["on"] is False
        finally:
            client.close()
            await tcp.stop()
            await web.stop()

    try:
        asyncio.run(run())
    finally:
        audio.close()
        hub.stop()


def test_web_methods_need_the_web_viewer():
    dispatcher = ApiDispatcher(_DummyHardware(), Config())
    with pytest.raises(ValueError, match="web viewer"):
        asyncio.run(dispatcher.dispatch("recording_status", {}))
