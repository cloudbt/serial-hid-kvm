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

### Live captions (`CC` button) and meeting minutes
- Client: `ccConfigure` (from `hello.captions`), `ccToggle` → `captions {on}`
  (remembered in localStorage, re-sent on every `hello`), `ccCaption` keeps the
  last 3 entries in `#ccLines` (CSS clips to 2 lines from the top), history in
  `#ccHistory` (copy / .txt). Minutes: `#recMinutes` select (localStorage),
  sent as `rec_session_start.minutes`; `minutesLine(item)` renders status,
  links and Retry in the recording list / result card; `renderMinutesList`
  shows a separate list when the SOP library hides the plain list.
- Server: `CaptionService` (`_captions.py`), `MinutesService` (`_minutes.py`),
  `_set_captions` / `_send_captions` per client, `_queue_minutes` at
  `rec_session_finish`, `_serve_minutes` route.
- Config: `ai_hub_url`, `ai_hub_key` (env only), `captions_enabled`,
  `captions_language`, `minutes_enabled`, `minutes_summarizer`,
  `minutes_fallback_order`, `minutes_timeout`, `mac_llm_url`, `mac_llm_key`
  (env only), `mac_llm_model`, `codex_executable`; agy reuses
  `sop_agy_executable`.
- Tests: `tests/test_captions.py`, `tests/test_minutes.py`;
  `tests/conftest.py` keeps every test off the real hub/LLM/`~/Videos`.

### Docked side panel, display size, live meeting points (captions v2)
- Layout is driven by CSS variables on `:root`: `--side-w` (docked panel
  width, 0 when closed or <= 600 px), `--tb-h` (toolbar height, 0 in
  fullscreen auto-hide / hidden toolbar), `--bar-h` (forgot-to-record bar).
  `#container` = `calc(100% - --side-w)` x `calc(100% - --tb-h - --bar-h)`,
  top-left aligned. `updateLayout()` recomputes them (resize, fullscreen,
  body class changes, toolbar ResizeObserver, panel drag) and a ResizeObserver
  on `#container` re-runs `updateCanvasSize()` so Fit follows the area.
- Display > Size (`#displayZoom`, localStorage `kvmDisplaySize`): Fit or a
  factor of the source width (`displayZoom`); the 1:1/Fit button toggles
  between Fit and the last fixed size.
- Side panel (`#sidePanel`): `#ccHistory` (top) + `#livePoints` (bottom),
  `#sideSplit` ratio (`kvmSideRatio`), `#sideResize` width (`kvmSideWidth`),
  fold state per section (`kvmSideFold:<id>`), open state (`kvmSideOpen`).
- Forgot-to-record bar (`#meetingBar`): CC live + finals for >= 2 min + this
  browser not recording; 本次忽略 is per CC session (sessionStorage).
- Tests: `tests/test_live_meeting.py`, `tests/test_web_display.py`
  (zoom/Fit/panel widths keep the 0-4095 mapping).
