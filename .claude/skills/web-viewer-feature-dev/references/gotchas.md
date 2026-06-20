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
  decoded then re-encoded. That CPU cost is why the server stream can't match a
  native `getUserMedia` `<video>` at high resolution. Don't try to fix smoothness
  by tuning fps/quality; offer Direct mode instead (local only).

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
