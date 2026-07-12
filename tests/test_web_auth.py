"""Tests for the web viewer's WebSocket authentication gate.

Runs a real WebViewerServer (with dummy hardware) on a loopback port and
drives the auth protocol with a websockets client.
"""

import asyncio
import json
import socket

import websockets

from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config


class _DummyCapture:
    def start_capture_thread(self):
        pass

    def close(self):
        pass

    def get_frame_jpeg(self, quality, force_reencode=False):
        return None


class _DummyCh9329:
    def send(self, pkt):
        pass


class _DummyHardware:
    def __init__(self):
        self._cap = _DummyCapture()
        self._ch = _DummyCh9329()

    def get_capture(self):
        return self._cap

    def get_ch9329(self):
        return self._ch


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_server(**config_overrides) -> tuple[WebViewerServer, str]:
    config = Config()
    config.web_host = "127.0.0.1"
    config.web_port = _free_port()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    server = WebViewerServer(_DummyHardware(), config)
    server._cap_label = "dummy"  # skip slow device enumeration
    server._AUTH_FAIL_DELAY_S = 0.01
    return server, f"ws://127.0.0.1:{config.web_port}/ws"


async def _recv_json(ws, timeout=5.0) -> dict:
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(msg, str):
            return json.loads(msg)


def test_no_password_hello_immediately():
    async def run():
        server, url = _make_server()
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "hello"
        finally:
            await server.stop()

    asyncio.run(run())


def test_password_flow_and_token_reauth():
    async def run():
        server, url = _make_server(web_password="s3cret")
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_required"

                # Non-auth messages are dropped while unauthenticated.
                await ws.send(json.dumps({"type": "keydown", "code": "KeyA"}))

                await ws.send(json.dumps({"type": "auth", "password": "nope"}))
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_failed"
                assert not msg.get("expired")

                # Retry with the right password on the same connection.
                await ws.send(json.dumps({"type": "auth", "password": "s3cret"}))
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_ok"
                token = msg["token"]
                assert token
                msg = await _recv_json(ws)
                assert msg["type"] == "hello"

            # Token re-auth on a fresh connection (silent reconnect).
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_required"
                await ws.send(json.dumps({"type": "auth", "token": token}))
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_ok"
                msg = await _recv_json(ws)
                assert msg["type"] == "hello"

            # Stale token (e.g. server restarted) → expired, then password.
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_required"
                await ws.send(json.dumps({"type": "auth", "token": "bogus"}))
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_failed"
                assert msg.get("expired") is True
                await ws.send(json.dumps({"type": "auth", "password": "s3cret"}))
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_ok"
        finally:
            await server.stop()

    asyncio.run(run())


def test_lockout_after_repeated_failures():
    async def run():
        server, url = _make_server(web_password="s3cret")
        await server.start()
        try:
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_required"
                for i in range(server._AUTH_MAX_FAILS):
                    await ws.send(json.dumps(
                        {"type": "auth", "password": f"wrong{i}"}))
                    msg = await _recv_json(ws)
                    assert msg["type"] == "auth_failed"
                # Connection is closed after the lockout-triggering failure.
                try:
                    await asyncio.wait_for(ws.recv(), 5)
                    raise AssertionError("expected the server to disconnect")
                except websockets.ConnectionClosed:
                    pass

            # New connections from the locked IP are rejected immediately,
            # even with the correct password.
            async with websockets.connect(url) as ws:
                msg = await _recv_json(ws)
                assert msg["type"] == "auth_failed"
        finally:
            await server.stop()

    asyncio.run(run())
