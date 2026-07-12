# Add-a-Feature Recipe

Most viewer features touch the same five spots. Do only the ones a feature needs.

## 1. Toolbar button (client, in `_VIEWER_HTML`)
Add to the `#toolbar` div, mirroring existing buttons:
```html
<button id="btnX" title="...">Label</button>
```
Active/toggle styling reuses `.active`; add a custom rule near `#btnRec.recording`
if needed. Wire it in JS:
```js
document.getElementById("btnX").addEventListener("click", () => { ...; container.focus(); });
```
Always `container.focus()` after a button click so keyboard capture returns.

## 2. Client JS behavior (in the `<script>`)
- Read/draw state lives at top of the script (globals).
- `send(obj)` → input gated by view-only. `wsSend(obj)` → raw control msg.
- For anything geometry-related, use `activeEl()/mediaW()/mediaH()` so it works
  in both canvas and Direct `<video>` mode. Input listeners are on `#container`.

## 3. WebSocket message (contract)
See websocket-protocol.md. Outbound from client: `wsSend({type:"x", ...})` or a
binary frame with a new tag byte. Inbound parse: add a branch in `ws.onmessage`.

## 4. Server handler (in `WebViewerServer`)
- Inbound control/input → add a branch in `_recv_input` (it has the per-client
  `state` dict and the coalescing sender). Run blocking hardware calls via
  `loop.run_in_executor(...)`.
- New outbound stream → a new `asyncio.create_task(self._send_x(ws, ...))` in
  `_handle_client`, looping with `await ws.send(b"\xNN" + data)` and catching
  `websockets.ConnectionClosed`.

## 5. Config setting (if user-tunable) — 4 edits in `config.py`
1. `Config.__init__`: `self.my_opt = <default>`
2. add `"my_opt"` to `_FILE_KEYS`
3. add `"SHKVM_MY_OPT": "my_opt"` to `_ENV_MAP`
4. add to `_apply_args` map (+ a CLI flag in `server.py:_build_parser`)
Then optionally surface it in the launcher (`Start-NanoKvmKvmApi.ps1`) as a param.

## 6. Validate (mandatory) + restart
Run the `py_compile` + `node --check` block from SKILL.md. Restart server, reload.

---

# Catalog of implemented features (mirror these)

### Screen recording (`Record` button)
- Client: `MediaRecorder` on `captureDisplayStream()` (canvas or `<video>`) +
  audio from `recDest`; chunks sent as binary `0x10`; MM:SS timer; red pulse.
- Server: `_recv_input` handles `rec_start`/`rec_stop` + `0x10` writes;
  `_open_recording()` sanitizes filename into `config.recording_dir` (.webm).
- Config: `recording_dir` (default `~/Videos`). Launcher: `-RecordingDir`.

### H264 / WebRTC video (`H264` button)
- Client: `RTCPeerConnection` (recvonly, non-trickle: full-SDP offer after ICE
  gathering) → `webrtc_offer` over the WS; answer applied from `webrtc_answer`;
  remote track rendered in the same `<video>` element Direct uses
  (`rtcMode` + `videoMode`); `?rtc=1` auto-starts; reconnect re-offers in
  `ws.onopen` (`restartRtc`); jitterBufferTarget/playoutDelayHint set to 0.
- Server: `_start_webrtc`/`_stop_webrtc` in `_web_viewer.py`; `_webrtc.py` has
  `WebRtcSession` (aiortc pc, H264-first codec prefs set BEFORE
  setRemoteDescription — aiortc negotiates codecs inside it) and
  `CaptureVideoTrack` (paced fps, skips duplicate frames via
  `capture.get_frame_if_newer`, even-trims odd autocrop sizes).
  **Server keeps the capture device** (unlike Direct); only that client's JPEG
  stream is paused via `state["webrtc"]` + `_update_frame_gate`. Encoder
  bitrate caps are module constants in aiortc, raised by
  `_raise_encoder_bitrate_caps`.
- Config: `webrtc_fps` (60), `webrtc_bitrate` (8 Mbps). Launcher: `-WebRtcFps`
  / `-WebRtcBitrate`. Optional dep: `serial-hid-kvm[webrtc]` → aiortc; hello
  message advertises availability (`webrtc: bool`).

### Direct (native) video (`Direct` button)
- Client: `getUserMedia` → `<video srcObject>`; device picked by matching
  `serverCaptureLabel` (from `capture_device` msg) in `enumerateDevices()`;
  `openCaptureRetry` handles the device-release race; reconnect re-syncs in
  `ws.onopen`.
- Server: `stream {on}` → `_acquire_stream`/`_release_stream`; `_stream_count`
  ref-counts the device; `_release_stream` calls `capture.close()` (real
  release); `_send_frames` pauses on `state["event"]`; `_capture_label()` reports
  the device name. `capture.py` loop self-heals (reopens with retries).
- Why: server JPEG transcode can't match a native MediaStream; only works when
  the browser is local to the dongle.

### Performance tuning
- Client: `createImageBitmap` + drop-stale rendering; trailing-send mouse
  throttle (~60Hz).
- Server: deadline-paced `_send_frames`; coalescing sender in `_recv_input`
  (consecutive moves collapse; clicks/keys ordered; abs packets self-position).
- Launcher: `-WebFps`/`-WebQuality`; `-Target` profiles set capture+screen res so
  the captured frame is a full-screen 1:1 view (correct cursor mapping).

### Toolbar show/hide hotkey (Ctrl+Alt+Enter)
- Default hidden via `body.tb-hidden`; `toggleToolbar()` flips it + `#hint` toast.
- Combo captured at `window` capture-phase keydown/keyup and **swallowed**
  (`stopPropagation`+`preventDefault`) so it never reaches the target.
- Important status (Saved / Direct failed) mirrored to `#hint` when toolbar hidden.

### Audio
- Server `AudioCapture` → `_send_audio` (binary `0x02`). Client AudioWorklet;
  playback muted via a `GainNode` (NOT `ctx.suspend`) so recording keeps audio.
