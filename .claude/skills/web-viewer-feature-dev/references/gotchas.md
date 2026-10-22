# Gotchas (hard-won — each one was a real bug)

## Capture device lifecycle
- `ScreenCapture.stop_capture_thread()` only stops the background thread; the
  OpenCV `VideoCapture` **stays open and keeps holding the device**. To actually
  free the device (so a browser in Direct mode can `getUserMedia`), call
  `close()` (it stops the thread *and* `cap.release()`).
- Symptom of getting this wrong: browser shows `Direct failed: Device in use`
  while the server stream is still running (fps counter still ticking).
- `start_capture_thread()` and the capture loop were made **self-healing**: they
  try `_ensure_open()` and retry on failure instead of crashing, so a transient
  "device busy" during hand-off recovers on its own.
- On Windows MSMF/DSHOW there is **no MJPEG passthrough** — every frame is
  decoded then re-encoded. That CPU cost is why the server JPEG stream can't
  match a native `getUserMedia` `<video>` at high resolution. Don't try to fix
  smoothness by tuning fps/quality; use the H264 (WebRTC) mode — server keeps
  the device, works remotely — or Direct mode (local only, device handed to
  the browser).
- aiortc negotiates codecs **inside `setRemoteDescription`** — call
  `addTrack` + `setCodecPreferences` BEFORE it, or the H264 preference is
  silently ignored and you get VP8. Its encoder bitrate caps
  (`aiortc.codecs.h264/vpx: DEFAULT/MIN/MAX_BITRATE`) are module constants
  clamping REMB feedback at runtime; raise MAX (or 1080p looks mushy) AND
  floor MIN (or REMB dips cause scroll blur + encoder rebuilds on every
  >10% target change).
- **Never await a device open/close on the WS message loop.** MSMF open
  takes ~25-30 s (cold AND after a direct-mode hand-back). Awaiting it in
  `_acquire_stream` froze `_recv_input`, so a `webrtc_offer` queued behind
  it timed out client-side — the "H264 needs two clicks" bug. Open/close
  run as background tasks serialised by `_device_lock` (with a
  `_stream_count` re-check under the lock so release/open can't interleave).
- **Pair WebRTC answers with their offer** (`gen` counter echoed by the
  server). A late answer applied to a newer RTCPeerConnection puts it in a
  broken state ("wrong state: stable" on the next real answer).
- The toggle-failure class of bug is reproducible without hardware clicks:
  headless Chrome + CDP `Runtime.evaluate` clicking toolbar buttons (see
  SKILL.md E2E section; add `--use-fake-ui-for-media-stream` to exercise
  Direct mode's getUserMedia hand-off).

## Embedded frontend (`_VIEWER_HTML`)
- It's a Python `r"""..."""` raw string containing HTML/CSS/JS. A stray `#` at
  the start of a line inside it is **not** a comment — it's literal text / a JS
  syntax error that kills the whole `<script>` (no video, no input). Use HTML
  `<!-- -->` and JS `//`. (A prior commit shipped `#`-commented buttons and broke
  the page.)
- Because it's a string, mistakes don't fail at import in the obvious place.
  ALWAYS run the `node --check` extraction from SKILL.md after editing.
- The input/cursor surface is `#container` (focusable, `tabindex=0`), not the
  canvas — the canvas is hidden in Direct mode. Keep listeners + `focus()` +
  cursor styling on `#container`. Use `activeEl()/mediaW()/mediaH()` for geometry.

## Input / cursor alignment
- Mapping invariant: `mouseCoords()` divides by `activeEl().getBoundingClientRect()`
  at event time, so ANY layout change (docked panel, left alignment, zoom,
  reminder bar, fullscreen) is safe as long as the element box equals the
  drawn picture: canvas stretches its bitmap, `<video>` must keep
  `object-fit: fill`. Never add padding/border/transform to `#screen`/`#video`
  or letterbox inside them. Synthetic `MouseEvent` clientX/Y are truncated
  to integers, so test probes must compute expectations from the integer
  coordinates they dispatch.
- Mouse uses **absolute** CH9329 packets with coords as a 0-4095 fraction of the
  active display. This is resolution-independent and is why host/target cursors
  line up — keep it fraction-based, never raw pixels.
- Coordinate mapping only stays correct if the captured frame is the **whole**
  target screen. If the capture resolution doesn't match the target's native
  output and the dongle crops, the cursor drifts (worse toward the bottom). The
  launcher's `-Target` profiles set capture+screen to the target's native res.
- Throttle moves but **trailing-send** the last position (otherwise the target
  settles a few px off when you stop). On the server, **coalesce** consecutive
  moves so serial never backs up; abs packets are self-positioning so reordering
  a coalesced move after a click is still correct.

## Direct mode device contention
- The capture device has exactly one owner: the server OR a browser, never both.
  `_stream_count` ref-counts server consumers; Direct mode drives it to 0 to
  release. While Direct is on, server-side `capture_frame`/OCR/preview can't grab
  frames — that's an inherent trade-off; document it, don't try to share.
- Hand-off races: browser `getUserMedia` is retried (`openCaptureRetry`); exiting
  Direct delays resuming the server stream (~400ms) and the capture loop
  self-heals; reconnect re-syncs by re-sending `stream:{on:false}` in `ws.onopen`.

## Audio
- Mute playback with a `GainNode` (`playGain.gain = 0`), NOT `audioCtx.suspend()`
  — suspend stops the whole graph, which would also kill audio captured for
  recording. `recDest` taps `audioNode` before the gain so recording always has
  sound regardless of the mute button.

- HDMI listening/recording "auto" input = this Target's capture card only
  when the browser runs on the Host (`recordingDefaultDevice`); anywhere else
  (phone, iPad) `recordingAutoDevice` falls back to `"server"` (the socket's
  `0x02` PCM) when `audioCfg` exists. Before that, phones got "Default …
  not available" and heard nothing.
- Server HDMI listening must play inside `audioCtx` (worklet →
  `serverMonitorGain` → destination, `openServerHdmiMonitor`). Bridging it
  through `recDest` (MediaStreamDestination) into the mixer's second
  context was silent on iPhone unless the Mic (a capture session) was open,
  and popped/crackled when it was. Recording still taps `recDest`; a local
  capture card still goes through the mixer.
- The HDMI worklet is a preallocated ring buffer (int16 in, converted in the
  worklet): 100 ms prebuffer (150 ms remote) at start and after every
  underrun, 5 ms fades in/out, oldest audio dropped above 300/400 ms. Never
  slice/concat per quantum (GC glitches). Stats arrive as `audioStats`.
  Tests: `tests/web_audio_worklet.cjs`.
- iOS `navigator.audioSession.type` (`syncAudioSession`): "playback" while
  only listening (speaker, ignores the silent switch), "play-and-record"
  while the Mic is on, "auto" otherwise.
- iOS starts an AudioContext only from inside the tap: the HDMI button
  creates the mixer context (`gestureAudioContext` → `mixer.open({context})`)
  and the server-PCM context (`unlockServerAudio` → memoised `startAudio()`,
  whose context is created synchronously) before the first `await`. With the
  Mic on, iOS plays through the earpiece (quiet) — the Mic hint says so.
- WASAPI streams (PortAudio/sounddevice): once any thread has opened a
  stream (e.g. the HDMI `AudioCapture` at startup), opening one on a thread
  WITHOUT COM initialised fails every time with "Unanticipated host error
  ... WdmSyncIoctl" (stale WDM-KS text) - input streams too. The mic uplink
  opens/closes only on its own single-thread `executor` whose initializer
  calls `CoInitializeEx` (`_uplink._init_com`, `_uplink_call`). Other code
  opening streams from worker threads (captions mic, recording mic) is
  exposed to the same failure.
- E2E for the uplink without a meeting: headless Chrome with
  `--use-file-for-fake-audio-capture=<wav>` (use a sweep/AM tone; Chrome's
  noise suppression eats steady tones and the default fake beep is too short),
  then grab TargetPC1 frames via the 9329 API while its Sound control panel
  Recording tab shows the Headset Microphone level meter.

## Launcher (`Start-NanoKvmKvmApi.ps1`)
- Audio auto-detection shells out to a slow PnP/WMI query (~3-4s), delaying the
  port bind. Use a **poll-until-deadline** readiness check, not a fixed
  `Start-Sleep`, or you get a false "did not listen on port" while the server is
  actually fine. Bail early on `$process.HasExited`.
- Profile-derived params: keep individual overrides working via
  `$PSBoundParameters.ContainsKey(...)`. Avoid the reserved automatic var name
  `$profile`.

## General
- Run blocking hardware calls (`ch9329.send`, `get_frame_jpeg`, file writes) via
  `loop.run_in_executor(...)` — never block the event loop.
- Frontend changes require a **server restart** (HTML is embedded and served with
  `Cache-Control: no-cache`, so a browser reload then gets fresh HTML).
