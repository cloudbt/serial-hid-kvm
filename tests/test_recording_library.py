"""Recording library confinement, completion states, and authenticated file access."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
import websockets
from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm._recording_session import RecordingSessions
from serial_hid_kvm._web_viewer import WebViewerServer
from tests.test_web_auth import _make_server, _recv_json


def test_completed_sessions_fallback_then_mp4_and_signed_links(tmp_path):
    sessions = RecordingSessions(tmp_path)
    session = 'a' * 32
    sessions.attach(session, 'recording-test.webm')
    sessions.write(session, 0, b'webm test')
    library = RecordingLibrary(tmp_path)
    assert library.items() == []
    source, destination = sessions.finish(session, 1)
    item = library.items()[0]
    assert item['name'].endswith('.webm')
    assert library.resolve_link(parse_qs(urlsplit(item['url']).query)) == source
    source.with_suffix('.conversion.log').write_text('failure')
    assert 'failed' in library.items()[0]['state']
    destination.write_bytes(b'mp4 test')
    final = library.items()[0]
    assert final['id'] == item['id']
    assert final['state'] == 'MP4 ready'
    assert library.resolve_link(parse_qs(urlsplit(final['url']).query)) == destination
    # Existing WebM links retain their original bytes after MP4 conversion.
    assert library.resolve_link(parse_qs(urlsplit(item['url']).query)) == source


def test_links_reject_tampering_expiry_and_escape(tmp_path):
    library = RecordingLibrary(tmp_path)
    (tmp_path/'recording.webm').write_bytes(b'data')
    query = parse_qs(urlsplit(library.items()[0]['url']).query)
    query['file'] = ['../outside.mp4']
    with pytest.raises(ValueError):
        library.resolve_link(query)
    expiry = str(int(time.time()) - 1)
    with pytest.raises(ValueError):
        library.resolve_link(dict(file=['recording.webm'], expires=[expiry], key=[library.sign('recording.webm', expiry)]))
    expiry = str(int(time.time()) + 60)
    with pytest.raises(ValueError):
        library.resolve_link(dict(file=['../outside.mp4'], expires=[expiry], key=[library.sign('../outside.mp4', expiry)]))


def test_recording_http_ranges_and_ws_auth(tmp_path):
    async def run():
        (tmp_path/'recording-demo.mp4').write_bytes(b'0123456789')
        server, url = _make_server(recording_dir=str(tmp_path), web_password='secret')
        await server.start()
        http_url = url.replace('ws:', 'http:').removesuffix('/ws')
        def get(link, headers=None):
            with urlopen(Request(http_url+link, headers=headers or {})) as response:
                return response.status, dict(response.headers), response.read()
        try:
            # A file request without a signed URL cannot bypass WebSocket auth.
            with pytest.raises(HTTPError) as exc:
                await asyncio.to_thread(get, '/recordings/file?file=recording-demo.mp4')
            assert exc.value.code == 404
            async with websockets.connect(url) as ws:
                assert (await _recv_json(ws))['type'] == 'auth_required'
                await ws.send(json.dumps(dict(type='auth', password='secret')))
                while (await _recv_json(ws))['type'] != 'hello':
                    pass
                await ws.send(json.dumps(dict(type='recordings_list')))
                while True:
                    message = await _recv_json(ws)
                    if message['type'] == 'recordings_list':
                        break
                assert not message['local']  # no browser Origin header
                item = message['items'][0]
                status, headers, body = await asyncio.to_thread(get, item['url'], {'Range':'bytes=2-5'})
                assert status == 206 and body == b'2345'
                assert headers['Content-Range'] == 'bytes 2-5/10'
                assert (await asyncio.to_thread(get, item['url'], {'Range':'bytes=-2'}))[2] == b'89'
                with pytest.raises(HTTPError) as invalid:
                    await asyncio.to_thread(get, item['url'], {'Range':'bytes=20-'})
                assert invalid.value.code == 416
                status, headers, body = await asyncio.to_thread(get, item['url']+'&download=1')
                assert status == 200 and body == b'0123456789'
                assert headers['Content-Disposition'].startswith('attachment')
                await ws.send(json.dumps(dict(type='recordings_open', id=item['id'], action='open')))
                while True:
                    reply = await _recv_json(ws)
                    if reply['type'] == 'recordings_action':
                        break
                assert 'only' in reply['error']
        finally:
            await server.stop()
    asyncio.run(run())
