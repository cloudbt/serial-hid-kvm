---
name: web-viewer-feature-dev
description: "Use when adding or changing features in the serial-hid-kvm browser web viewer — toolbar buttons, video/audio streaming, screen recording, Direct (native getUserMedia) video, keyboard/mouse input, the embedded HTML/JS, the WebSocket protocol, or the Start-NanoKvmKvmApi.ps1 launcher. Gives the architecture, the WebSocket contract, an end-to-end add-a-feature recipe, the mandatory py_compile + node --check validation workflow, and the hard-won gotchas (capture-device release, direct-mode device contention, embedded-JS comment rules, input coalescing)."
---

# serial-hid-kvm Web Viewer — Feature Development

A browser KVM served by a Python process: it streams the HDMI capture to the
browser and forwards keyboard/mouse back to a CH9329 USB-HID emulator. The
entire frontend (HTML + CSS + JS) is **embedded as one raw string** in
`_web_viewer.py` — no npm, no build step.

Use this skill to add features the way the existing ones were added (recorder,
Direct mode, toolbar hotkey, perf tuning) so you don't re-explore the codebase.

## File map (serial-hid-kvm/src/serial_hid_kvm/)

- `_web_viewer.py` — **the main file.** `_VIEWER_HTML` (embedded frontend) +
  `WebViewerServer` (WebSocket + HTTP on one port). Almost all viewer features
  live here, on both sides of the socket.
- `capture.py` — `ScreenCapture`: HDMI grab via OpenCV, JPEG encode, autocrop,
  MJPEG passthrough. Read the gotchas before touching device lifecycle.
- `_webrtc.py` — optional H264/WebRTC streaming (`WebRtcSession`,
  `CaptureVideoTrack`); server keeps the device (unlike Direct). Needs the
  `serial-hid-kvm[webrtc]` extra (aiortc). Signaling rides the `/ws` socket.
- `hid_protocol.py` — `CH9329` serial + packet builders (`build_keyboard_report`,
  `build_mouse_abs_packet` [coords 0-4095], `build_mouse_rel_packet`).
- `_audio.py` — `AudioCapture` (PCM broadcast to subscriber queues).
- `config.py` — `Config` + load order (CLI > env `SHKVM_*` > YAML > default).
  Adding a setting = 4 edits (see references/feature-recipe.md).
- `server.py` — CLI parser, `KvmHardware`, process wiring (`--headless`, `--web`).

Launcher (separate repo): `NanoKVM-USB/ai/scripts/Start-NanoKvmKvmApi.ps1`
— `-Target` resolution profiles, `-Web -WebFps -WebQuality -RecordingDir`,
poll-for-port startup.

## Architecture / data flow

```
Browser  ──WebSocket(/ws)──  WebViewerServer (_web_viewer.py)  ──  KvmHardware
 <video>/<canvas>             _send_frames / _send_audio              capture.py (HDMI)
 input listeners              _recv_input                             hid_protocol.py (CH9329)
```

- **Video (default):** server `get_frame_jpeg` → binary `0x01`+JPEG → browser
  `createImageBitmap` → `<canvas>` (drops stale frames; renders freshest).
- **Video (H264/WebRTC):** server encodes the same capture as H.264 (aiortc)
  → `RTCPeerConnection` → native `<video>`. Server **keeps the device**;
  only that client's JPEG stream is paused (`state["webrtc"]` +
  `_update_frame_gate`). Works remotely; OCR/`capture_frame` keep working.
- **Video (Direct):** browser opens the capture card itself via `getUserMedia`
  → native `<video>`. Server **releases the device** (`_release_stream` →
  `capture.close()`) so the browser can open it. Local-only; conflicts with
  server-side OCR/`capture_frame`.
- **Audio:** server PCM → binary `0x02` → AudioWorklet. A `GainNode` mutes
  playback without stopping capture (so recording still gets audio).
- **Input:** browser JSON → `_recv_input`. A background **sender task**
  coalesces consecutive mouse moves and keeps clicks/keys ordered, so serial
  never backs up. Mouse coords are a 0-4095 fraction of the active display.
- **Recording:** browser `MediaRecorder` (canvas or `<video>`.captureStream +
  audio) → binary `0x10`+webm chunks → server writes to `config.recording_dir`.
  No save dialog.

The shared input/cursor surface is `#container` (NOT the canvas), so input works
in both canvas and `<video>` mode. Use `activeEl()/mediaW()/mediaH()` for
mode-agnostic geometry.

## WebSocket contract

The message protocol is the integration contract — read
**references/websocket-protocol.md** before adding any message type or binary tag.

## Adding a feature

Follow **references/feature-recipe.md** — the end-to-end pattern (toolbar button →
client JS → WS message → server handler → optional config/launcher knob) plus a
catalog of the already-implemented features to mirror.

## Gotchas (READ before editing — these cost real debugging)

Full detail in **references/gotchas.md**. The non-negotiables:

1. **`stop_capture_thread()` does NOT release the device** — only `close()` does.
   Releasing for Direct mode must use `close()`.
2. **No `#` comments inside `_VIEWER_HTML`.** It's HTML/JS; `#`-prefixed lines
   are a syntax error that breaks the *entire* `<script>`. Use `<!-- -->` / `//`.
3. **Direct mode = device contention.** Only the browser OR the server can hold
   the capture device, never both. Ref-count via `_stream_count`.
4. **Coalesce mouse moves**, abs packets are self-positioning, trailing-send the
   last move (else cursor settles offset).
5. **Launcher: poll for the port, never a fixed sleep** (audio auto-detect adds
   ~3-4s to startup).

## MANDATORY validation (no hardware needed)

The frontend is embedded as a Python string, so a typo silently ships. ALWAYS
run this after editing `_web_viewer.py`:

```bash
cd serial-hid-kvm
python -m py_compile src/serial_hid_kvm/_web_viewer.py && echo "PY OK"
python -c "
import re
src = open('src/serial_hid_kvm/_web_viewer.py', encoding='utf-8').read()
m = re.search(r'_VIEWER_HTML = r\"\"\"(.*?)\"\"\"', src, re.S)
js = re.search(r'<script>(.*)</script>', m.group(1), re.S).group(1)
open('viewer_check.js','w',encoding='utf-8').write(js)
" && node --check viewer_check.js && echo "JS OK" && rm -f viewer_check.js
```

Also parse-check the launcher after editing it:

```powershell
$null = [System.Management.Automation.Language.Parser]::ParseFile("<path>.ps1", [ref]$null, [ref]$null); "PS1 OK"
```

## Apply / test loop

Frontend changes are served by the running process, so **restart the server** and
reload the browser (HTML is sent `Cache-Control: no-cache`):

```powershell
Get-NetTCPConnection -LocalPort 9329 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }
./ai/scripts/Start-NanoKvmKvmApi.ps1 -Web -NoInstall
```

For a real E2E check without touching the user's session, drive a **headless
Chrome** against the live server and read the server log (proven to work for
the WebRTC/H264 path — `?rtc=1` auto-starts it):

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --headless=new `
  --disable-gpu --mute-audio --user-data-dir=$env:TEMP\kvm-e2e-profile `
  --autoplay-policy=no-user-gesture-required "http://127.0.0.1:9330/?rtc=1"
# then poll ai/logs/serial-hid-kvm-api.err.log for the expected lines
```

Caveats: first run of a fresh profile takes ~15 s before the WS even opens,
and a cold MSMF device open can add ~25 s — poll the log with a generous
deadline. Kill the chrome process when done. For interactive confirmation
(input feel, latency) still ask the user, requesting the status-bar text /
server log on failure.
