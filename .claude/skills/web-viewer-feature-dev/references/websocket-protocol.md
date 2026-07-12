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

Send pattern (server): `await ws.send(b"\x01" + jpeg_bytes)`.
Read pattern (client): `view[0]` is the tag, `ev.data.slice(1)` is payload.
Client→server binary is handled at the top of `_recv_input` (checks
`message[0] == 0x10`).

## Text (JSON) messages

### client → server
| type | fields | handler effect |
|------|--------|----------------|
| `keydown` | `code` (W3C KeyboardEvent.code) | mapped via `_JS_CODE_TO_HID` / `_JS_MOD_BITS` → keyboard report |
| `keyup` | `code` | release key/modifier |
| `mousemove` | `x`,`y` (0-4095), `buttons` | abs mouse packet (coalesced) |
| `mousedown` | `x`,`y`,`buttons` | abs mouse packet (ordered) |
| `mouseup` | `x`,`y`,`buttons` | abs mouse packet (ordered) |
| `scroll` | `deltaY` | rel packet with scroll |
| `release_all` | — | clear all held keys/buttons |
| `stream` | `on` (bool) | acquire/release the server JPEG stream + capture device |
| `webrtc_offer` | `sdp` (complete, ICE-gathered), `gen` | negotiate H.264 WebRTC stream; server keeps the device, pauses this client's JPEG; replies `webrtc_answer` or `webrtc_error` echoing `gen` |
| `webrtc_stop` | — | close the WebRTC session; JPEG stream resumes |
| `rec_start` | `filename` | open recording file under `recording_dir` |
| `rec_stop` | — | close file, reply `rec_saved` |

`send(obj)` is gated by view-only; `wsSend(obj)` is raw (use for control msgs
like `stream`, and recording uses `ws.send` directly).

### server → client
| type | fields | client effect |
|------|--------|---------------|
| `hello` | `build`,`webrtc` (bool) | auto-reload check; disable H264 button if server lacks aiortc |
| `audio_config` | `sampleRate`,`channels` | enable Audio button, configure worklet |
| `capture_device` | `label` | Direct mode matches this device in `enumerateDevices()` |
| `webrtc_answer` | `sdp`, `gen` | complete answer SDP → `setRemoteDescription` (applied only if `gen` matches the current offer — late answers are dropped) |
| `webrtc_error` | `error`, `gen` | reject pending offer / show error (stale `gen` ignored) |
| `rec_saved` | `path` | show "Saved: …" |
| `rec_error` | `error` | show error |

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
