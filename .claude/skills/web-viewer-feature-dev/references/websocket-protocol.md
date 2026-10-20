# WebSocket Protocol (single `/ws` connection)

One WebSocket carries everything. **Binary** frames are media/recording (first
byte = a type tag). **Text** frames are JSON control messages (`{"type": ...}`).
Both directions share the socket; tags never collide because each direction owns
its own tag values.

Defined in `_web_viewer.py`: client side in the `_VIEWER_HTML` JS (`ws.onmessage`,
`send()`, `wsSend()`); server side in `WebViewerServer._send_frames`,
`_send_audio`, and `_recv_input`.

## Binary frames

| Direction | Tag byte | Payload | Meaning |
|-----------|----------|---------|---------|
| server→client | `0x01` | JPEG bytes | video frame |
| server→client | `0x02` | PCM int16 LE | audio chunk |
| client→server | `0x10` | WebM bytes | recording chunk (appended to file) |
| client→server | `0x11` | u32 BE sequence + WebM bytes | resumable recording-session block (acked by `rec_ack`) |

Send pattern (server): `await ws.send(b"\x01" + jpeg_bytes)`.
Read pattern (client): `view[0]` is the tag, `ev.data.slice(1)` is payload.
Client→server binary is handled at the top of `_recv_input` (checks
`message[0] == 0x10`).

## Text (JSON) messages

### client → server
| type | fields | handler effect |
|------|--------|----------------|
| `auth` | `password` or `token` | authenticate when the server runs with `--web-password`; MUST precede everything else on such servers (all other messages are dropped until authenticated). Replies `auth_ok` (with a reconnect `token`) or `auth_failed` |
| `keydown` | `code` (W3C KeyboardEvent.code) | mapped via `_JS_CODE_TO_HID` / `_JS_MOD_BITS` → keyboard report |
| `keyup` | `code` | release key/modifier |
| `mousemove` | `x`,`y` (0-4095), `buttons` | abs mouse packet (coalesced) |
| `mousedown` | `x`,`y`,`buttons` | abs mouse packet (ordered) |
| `mouseup` | `x`,`y`,`buttons` | abs mouse packet (ordered) |
| `scroll` | `deltaY` | rel packet with scroll |
| `release_all` | — | clear all held keys/buttons |
| `stream` | `on` (bool) | acquire/release the server JPEG stream + capture device |
| `frame_ack` | — | WAN adaptive stream: returns one in-flight frame credit (client sends it for every received `0x01` frame when `hello.adaptive` is true) |
| `audio` | `on` (bool) | subscribe/unsubscribe this client's PCM feed (WAN clients start unsubscribed; while off, no `0x02` frames are sent at all) |
| `webrtc_offer` | `sdp` (complete, ICE-gathered), `gen`, `bitrate?`, `fps?` | negotiate H.264 WebRTC stream; server keeps the device, pauses this client's JPEG; replies `webrtc_answer` or `webrtc_error` echoing `gen`. Optional `bitrate`/`fps` come from the toolbar quality selector and are clamped to config maxima; absent → LAN gets configured values (floor at half cap), WAN gets 30 fps / 4 Mbps start with free REMB adaptation (`_webrtc_params`) |
| `webrtc_stop` | — | close the WebRTC session; JPEG stream resumes |
| `srec_start` | `fps` (15/30), `source` (`mic`/`hdmi`/`mix`/`none`), `minutes?` | start a **server-side** recording (default mode): ffmpeg encodes the capture + Host mic (`recording_mic`) + server HDMI audio into `.recording-sessions/<id>/source.mkv`. Broadcast `srec_state` to every viewer; on failure reply `srec_error` + `srec_state` |
| `srec_stop` | — | stop the server recording; the recorder thread then saves it (same pipeline as `rec_session_finish`: minutes, SOP notice, MP4 export by remux) and broadcasts `rec_session_saved` (`server: true`) and later `rec_exported` |
| `rec_start` | `filename` | open recording file under `recording_dir` |
| `rec_stop` | — | close file, reply `rec_saved` |
| `rec_session_start` | `session` (32 hex), `filename`, `minutes?` (`agy`/`codex`/`mac-qwen`/`off`) | attach/resume a resumable recording session; `minutes` is stored in `session.json` on first attach (later attaches keep the original), together with `started_epoch` (server clock, origin of `.live.vtt`) |
| `rec_session_finish` | `session`, `next`, `metadata?` | finish; reply `rec_session_saved`; starts MP4 export AND queues meeting minutes (independent, minutes are NOT under `_conversion_lock`) |
| `captions` | `on` (bool) | join/leave the shared live-caption feed (Target HDMI → kvm-ai-hub). Reply on off: `captions_state off`; when unavailable: `captions_state unavailable` |
| `minutes_retry` | `id` (recording id), `summarizer?` | retry a failed minutes job from its last good step, re-summarize a done one with another summarizer, or create a job for an older recording; reply `minutes_action` |
| `recordings_list` | — | reply `recordings_list` (items carry `minutes` status; `meeting_notes` lists caption-only minutes) |
| `live_summary` | `action` (`run` / `set_interval` / `set_summarizer` / `minutes` / `sync`), `minutes?` (interval: 0/3/5/10), `summarizer?` (`agy`/`codex`/`mac-qwen`/`off`, accepted with any action) | live meeting points of the current CC session: `run` = summarize now (never concurrent; failures come back as `notice`; with CC off it (re)runs the final summary - this is the 重试 button), `set_interval` = server-wide auto-summary interval (persisted in `.captions/settings.json`), `minutes` = queue minutes from this session's captions; every action is answered/broadcast as `live_summary` |
| `captions_new_meeting` | — | finalize the current CC session and start a new one (broadcast `live_summary` with the new `session`) |

`send(obj)` is gated by view-only; `wsSend(obj)` is raw (use for control msgs
like `stream`, and recording uses `ws.send` directly).

### server → client
| type | fields | client effect |
|------|--------|---------------|
| `auth_required` | — | first message on password-protected servers: send stored token silently or show the login overlay |
| `auth_ok` | `token` | store token (sessionStorage + optional localStorage), hide overlay; session continues with `hello` |
| `auth_failed` | `error`, `expired?` | `expired: true` = stale token → clear it and prompt for password; otherwise show the error (wrong password / lockout) |
| `hello` | `build`,`webrtc` (bool),`adaptive` (bool),`captions` `{available, reason}`,`minutes` `{enabled, default}` | auto-reload check; disable H264 button if server lacks aiortc; `adaptive` tells a WAN client to send `frame_ack` per received frame. Also the "session started" signal — mode restore (direct/rtc re-setup after reconnect) runs here, NOT in `ws.onopen`, because pre-auth messages are dropped |
| `audio_config` | `sampleRate`,`channels`,`on` (bool) | enable Audio button, configure worklet; `on` is the server-side subscription state this client started with (LAN: true, WAN: false) |
| `capture_device` | `label` | Direct mode matches this device in `enumerateDevices()` |
| `webrtc_answer` | `sdp`, `gen` | complete answer SDP → `setRemoteDescription` (applied only if `gen` matches the current offer — late answers are dropped) |
| `webrtc_error` | `error`, `gen` | reject pending offer / show error (stale `gen` ignored) |
| `srec_state` | `active`, `starting`, `session`, `fps`, `source`, `encoder` (`h264_amf`/`libx264`), `elapsed` (s), `frames`, `bytes`, `audio_inputs`, `warnings[]`, `stopped_reason?` (`manual`/`max_duration`/`disk_full`/`error`/`shutdown`/`recovered`), `error?` | Record button state on every viewer (also in `hello.server_recording.state`); the timer runs from `elapsed` |
| `srec_error` | `error` | the start request failed (nothing recorded) |
| `srec_notice` | `message` | automatic stop (3 h limit, low disk, error) or a save failure |
| `rec_saved` | `path` | show "Saved: …" |
| `rec_error` | `error` | show error |
| `caption` | `seq`,`t0`,`t1` (s from hub stream start),`text`,`lang`,`final`,`at`/`end` (epoch ms),`replay?` | `final: false` = interim (overlay only; later interim and the final with the same `seq` replace it in place). Finals go to the overlay (max 2 lines) and history. `replay: true` = recent finals re-sent on subscribe (history only) |
| `live_summary` | `state` (`idle`/`running`/`failed`), `final` (bool: the running/latest summary is the final one made after CC was turned off; on `failed` = the final summary failed), `data` (`{current_topic, points[], decisions[], todos[{task,owner,due}], open_questions[], new_since_last[]}` or null), `previous` (the summary before, for highlighting), `at` (ms), `used`, `error`, `interval`, `summarizer`, `session` `{id, started_at, finals, recorded, summarized, final_pending, closed}` (`closed`: no session is open; this is the newest finalized session, shown so its final summary stays visible), `minutes` (caption-minutes job view or null), `notice?` | render the meeting-points section; badge on CC when a new summary arrives while the section is hidden. Sent to EVERY authenticated viewer (CC on or off), once at connect and on every change |
| `captions_state` | `state` (`off`/`connecting`/`live`/`offline`/`unavailable`), `detail` | CC button styling, offline notice over the video |
| `minutes_action` | `id`, `message` or `error` | hint + refresh recordings list |
| `recordings_list` | `items[]` (each `minutes`: null or `{state, label, summarizer, used, error, links{minutes, minutes_md, transcript, live_vtt}}`), `minutes_enabled`, `local`, `sop_enabled`, `sop_prompt` | render list / result card |

## LAN vs WAN split

Both sides classify the link independently and the behaviours pair up:
the server checks the connection's source address (`_is_private_address` /
`_client_is_wan`, overridable in tests), the client checks
`isPrivateHost(location.hostname)` (`IS_LAN`). LAN keeps the original
loopback tuning: fixed-pace JPEG at `web_fps`/`web_quality`, always-on audio,
zero WebRTC jitter buffer, H264 bitrate floored at half the cap. WAN gets:
credit-paced adaptive JPEG (`_WAN_*` constants; quality follows achieved
fps), audio off until requested, browser-default jitter buffer, and REMB
free to drop the H264 rate (per-encoder bounds stamped by
`WebRtcSession._stamp_encoder` via the patched `target_bitrate` setter).

## Conventions when extending

- Pick the next free tag byte for a new binary stream; keep server→client and
  client→server tag spaces from overlapping in meaning.
- Prefer text JSON for control/metadata; reserve binary for bulk media.
- Add the client parse branch in `ws.onmessage` (text) or the `instanceof
  ArrayBuffer` block (binary), and the server branch in `_recv_input` (inbound)
  or a `_send_*` task (outbound).
- Mouse coordinates are always a **0-4095 fraction of the active display**
  (`mouseCoords()` uses `activeEl().getBoundingClientRect()`), never raw pixels —
  this is what keeps the host and target cursors aligned across resolutions.

## Live captions and meeting minutes (kvm-ai-hub)

- `_captions.py` `CaptionService` (one per process): subscribes to the server
  `AudioCapture` (Target HDMI), `Resampler` → mono 16 kHz s16le, 100 ms binary
  frames to hub `WS /v1/captions` (`start` → `ready`, PCM, `stop`;
  hub sends `caption`/`speech`/`error`). Ref-counted by viewer subscriptions
  (asyncio queues) + 5 s linger; exponential backoff reconnect (≤30 s). Hub
  key goes in the `Authorization` header and is scrubbed from state details.
- `_minutes.py` `MinutesService`: background thread, jobs in
  `recording_dir/.minutes/<recording_id>/job.json` (+ `lock` heartbeat for
  processes sharing the folder). Steps: ffmpeg FLAC → hub
  `POST /v1/jobs/transcribe` + poll `GET /v1/jobs/{id}` → `transcript.json`
  + `<rec>.srt` → summarizer chain → `minutes.json` → `<rec>.minutes.md`.
  Each step's output is a resume point.
- Signed links (`RecordingLibrary.file_url`) now also serve `.md/.srt/.vtt`
  as `text/plain`; `/recordings/minutes?<signed .minutes.md>` renders an
  escaped HTML page (nonce CSP) with the video and seekable timestamps.
- Mock hub for tests/dev: `tests/mock_ai_hub.py`.

### CC sessions and live meeting points (`_live_meeting.py`)

- A CC session starts with the first CC subscriber. Final captions are
  appended to `recording_dir/.captions/<YYYYmmdd-HHMMSS>-<id>.jsonl`; state and
  every summary live in `.captions/<id>/` (`meta.json`, `summary-NNNN.json`).
- The hub link stops after LINGER, but the session stays resumable for
  `RESUME_WINDOW` (10 min: reloads, KVM restart). After that, or on
  `captions_new_meeting`, it is finalized; an unrecorded session with
  >= 10 finals over >= 2 min gets caption-only minutes in `meeting-notes/`
  (skipped when a recording overlapped or 生成会议记录 already covered it).
- Auto summary: every `interval` minutes (first one one interval after the
  session starts), only if new finals arrived, never concurrently.
  Summarizer = the viewer's 会议记录 choice (`set_summarizer`), same fallback
  chain as minutes; `mac-qwen` never falls back to a cloud CLI.
- Interim captions (`final: false`) never reach history, `.live.vtt` or jsonl.

### Final summary when CC is turned off

- `CaptionService.on_stopped` fires after the last viewer left, LINGER passed
  and the hub flushed the last sentence. If the session has unsummarized
  finals and the summarizer is not `off`, `meta.json` gets
  `final_pending: true` and a server task runs one final summary - also with
  the auto interval off, and whether or not any page is still open.
- It waits for a summary that is already running and then tops up while
  finals remain unsummarized. Reopening CC during it turns the result into an
  ordinary summary (`final: false`) and the meeting continues.
- Saved as `summary-NNNN.json` with `"final": true`; `final_pending` is
  cleared afterwards. A pending final summary is resumed at server start.
- The 10-minute finalize waits while a final summary is running.
