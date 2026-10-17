"""Only observations may be replayed after a lost TCP response; no hardware."""

from unittest.mock import Mock

import pytest

from serial_hid_kvm.client import KvmClient, KvmClientError


@pytest.mark.parametrize("method", ["mouse_click", "mouse_down", "mouse_up", "mouse_move",
    "mouse_scroll", "send_key", "type_text", "send_key_sequence"])
@pytest.mark.parametrize("failure", [ConnectionError("lost response"), OSError("send failed")])
def test_input_requests_never_replayed(method, failure, monkeypatch):
    client = KvmClient()
    sock, reader = Mock(), Mock()
    client._sock, client._rfile = sock, reader
    reader.readline.side_effect = failure
    reconnect = Mock(side_effect=AssertionError("must not reconnect/replay"))
    monkeypatch.setattr(client, "_connect_unlocked", reconnect)
    with pytest.raises(KvmClientError):
        client.call(method, {})
    sock.sendall.assert_called_once()
    reconnect.assert_not_called()
    assert not client.connected


def test_capture_can_reconnect_once(monkeypatch):
    client = KvmClient()
    old_sock, old_reader = Mock(), Mock()
    client._sock, client._rfile = old_sock, old_reader
    old_reader.readline.return_value = ""
    new_sock, new_reader = Mock(), Mock()
    new_reader.readline.return_value = '{"ok":true,"result":{"jpeg_b64":"abc"}}'

    def connect():
        client._sock, client._rfile = new_sock, new_reader

    reconnect = Mock(side_effect=connect)
    monkeypatch.setattr(client, "_connect_unlocked", reconnect)
    monkeypatch.setattr("serial_hid_kvm.client.time.sleep", lambda _: None)
    assert client.call("capture_frame") == {"jpeg_b64": "abc"}
    old_sock.sendall.assert_called_once()
    new_sock.sendall.assert_called_once()
    reconnect.assert_called_once()


def test_malformed_input_response_does_not_replay(monkeypatch):
    client = KvmClient()
    sock, reader = Mock(), Mock()
    client._sock, client._rfile = sock, reader
    reader.readline.return_value = "invalid JSON\n"
    reconnect = Mock()
    monkeypatch.setattr(client, "_connect_unlocked", reconnect)
    with pytest.raises(KvmClientError):
        client.send_key("enter")
    sock.sendall.assert_called_once()
    reconnect.assert_not_called()
