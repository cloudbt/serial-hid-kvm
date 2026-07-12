"""WebRTC (H.264) low-latency video streaming for the web viewer.

The browser receives a native ``<video>`` MediaStream (hardware-decoded
H.264 with WebRTC pacing/jitter handling) while the *server* keeps
ownership of the capture device — unlike Direct mode, which hands the
device to the browser.  Server-side consumers (API ``capture_frame``,
OCR, MCP) therefore keep working while the viewer streams.

Signaling rides on the existing web-viewer WebSocket (``webrtc_offer`` /
``webrtc_answer`` / ``webrtc_stop`` JSON messages, see _web_viewer.py).
On loopback/LAN links host candidates suffice; remote (WAN) viewers rely
on STUN server-reflexive candidates (aiortc defaults to Google STUN).

LAN and WAN sessions are tuned differently (see _web_viewer.py, which
picks the numbers per connection): LAN starts at the configured bitrate
with a floor at half of it — REMB dips would otherwise blur desktop text —
while WAN starts low and leaves REMB free to drop the rate to whatever the
uplink actually sustains, because a floored rate on a congested link turns
into packet loss and keyframe-request freezes.

Requires the optional aiortc dependency::

    pip install serial-hid-kvm[webrtc]
"""

import asyncio
import fractions
import logging

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
from av import VideoFrame

logger = logging.getLogger(__name__)

_CLOCK_RATE = 90000
_TIME_BASE = fractions.Fraction(1, _CLOCK_RATE)

_h264_encoder_ok: bool | None = None  # cached libx264 probe result


def _h264_available() -> bool:
    """Whether PyAV can create a libx264 encoder (probed once)."""
    global _h264_encoder_ok
    if _h264_encoder_ok is None:
        try:
            import av
            av.CodecContext.create("libx264", "w")
            _h264_encoder_ok = True
        except Exception as e:
            logger.warning(f"libx264 encoder unavailable, falling back to VP8: {e}")
            _h264_encoder_ok = False
    return _h264_encoder_ok


def _install_per_encoder_bitrate_clamps():
    """Patch aiortc encoders so bitrate bounds can be set per instance.

    aiortc clamps ``target_bitrate`` (driven by browser REMB congestion
    feedback) to *module-level* constants, which are process-global.  A
    LAN session wants a high floor (transient REMB dips visibly blur
    desktop text while scrolling), while a WAN session must let REMB drop
    the rate to what the uplink actually carries — a floored rate on a
    congested link means sustained packet loss and PLI/keyframe freezes.

    The patched setter honours ``_kvm_min_bitrate`` / ``_kvm_max_bitrate``
    attributes stamped onto the encoder instance by
    :meth:`WebRtcSession._stamp_encoder`; instances without them keep the
    stock module-constant clamping.  Idempotent.
    """
    from aiortc.codecs import h264, vpx
    for cls in (h264.H264Encoder, vpx.Vp8Encoder):
        prop = cls.target_bitrate
        if getattr(prop.fset, "_kvm_patched", False):
            continue
        orig_fset = prop.fset

        def fset(self, bitrate: int, _orig=orig_fset):
            lo = getattr(self, "_kvm_min_bitrate", None)
            hi = getattr(self, "_kvm_max_bitrate", None)
            if lo is not None:
                bitrate = max(lo, bitrate)
            if hi is not None:
                bitrate = min(hi, bitrate)
            _orig(self, bitrate)

        fset._kvm_patched = True
        setattr(cls, "target_bitrate", property(prop.fget, fset))


def _set_global_bitrate_bounds(start: int, cap: int):
    """Adjust aiortc's module-level bitrate constants for new encoders.

    aiortc's stock caps (H.264 3 Mbps / VP8 1.5 Mbps) are far too low for
    desktop content, so MAX is raised to at least *cap* (monotonically —
    concurrent sessions never shrink each other's ceiling).  DEFAULT is
    the rate an encoder starts at before :meth:`WebRtcSession._stamp_encoder`
    applies the per-session bounds; MIN is left at aiortc's stock values so
    per-instance floors (or their absence, for WAN) stay in charge.
    """
    from aiortc.codecs import h264, vpx
    for mod in (h264, vpx):
        mod.DEFAULT_BITRATE = start
        mod.MAX_BITRATE = max(cap, mod.MAX_BITRATE)


class CaptureVideoTrack(MediaStreamTrack):
    """Video track relaying frames from the shared :class:`ScreenCapture`.

    Reads the same latest-frame buffer the JPEG stream and the API use, so
    the device stays owned by the server.  Paced at *fps*, but a frame is
    only re-sent when the capture loop hasn't produced a new one within a
    frame period (the encoder handles duplicates cheaply; fresh frames are
    preferred to minimise latency).
    """

    kind = "video"

    def __init__(self, capture, fps: int):
        super().__init__()
        self._capture = capture
        self._interval = 1.0 / max(1, fps)
        self._t0: float | None = None
        self._next_t = 0.0
        self._last_seq = -1
        self._last_frame: np.ndarray | None = None

    async def recv(self) -> VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        loop = asyncio.get_running_loop()
        if self._t0 is None:
            self._t0 = loop.time()
            self._next_t = self._t0

        # Pace to the configured frame rate (deadline-based, like
        # _send_frames: encode time is absorbed into the interval).
        delay = self._next_t - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_t = max(self._next_t + self._interval,
                           loop.time() - self._interval)

        # Wait up to one frame period for a frame the capture loop hasn't
        # already given us, then fall back to repeating the previous one
        # (keeps the stream alive while the device is reopening).
        deadline = loop.time() + self._interval
        arr = None
        while True:
            arr, seq = self._capture.get_frame_if_newer(self._last_seq)
            if arr is not None:
                self._last_seq = seq
                self._last_frame = arr
                break
            if loop.time() >= deadline:
                arr = self._last_frame
                break
            await asyncio.sleep(0.002)

        if arr is None:
            # No frame ever captured yet — emit black so negotiation and
            # playback start immediately; real frames replace it as soon
            # as the capture loop delivers.
            arr = np.zeros((720, 1280, 3), dtype=np.uint8)

        # VideoFrame construction copies ~a full frame; keep it off the loop.
        frame = await loop.run_in_executor(None, self._to_video_frame, arr)
        frame.pts = int((loop.time() - self._t0) * _CLOCK_RATE)
        frame.time_base = _TIME_BASE
        return frame

    @staticmethod
    def _to_video_frame(arr: np.ndarray) -> VideoFrame:
        h, w = arr.shape[:2]
        if (w % 2) or (h % 2):
            # yuv420 needs even dimensions; autocrop can produce odd ones.
            arr = np.ascontiguousarray(arr[: h - (h % 2), : w - (w % 2)])
        return VideoFrame.from_ndarray(arr, format="bgr24")


class WebRtcSession:
    """One peer connection streaming the capture to one viewer client.

    Args:
        capture: shared ScreenCapture.
        fps: stream frame rate for this session.
        bitrate: bitrate ceiling (REMB may not push above it).
        start_bitrate: encoder starting rate (defaults to *bitrate*).
        min_bitrate: floor for REMB dips; ``None`` leaves aiortc's stock
            minimum so congestion feedback can drop the rate freely (WAN).
        on_closed: callback fired once when the peer connection dies.
    """

    def __init__(self, capture, fps: int, bitrate: int,
                 start_bitrate: int | None = None,
                 min_bitrate: int | None = None,
                 on_closed=None):
        _install_per_encoder_bitrate_clamps()
        self._cap_bitrate = bitrate
        self._start_bitrate = start_bitrate or bitrate
        self._min_bitrate = min_bitrate
        _set_global_bitrate_bounds(self._start_bitrate, bitrate)
        self._capture = capture
        self._fps = fps
        self._pc: RTCPeerConnection | None = None
        self._closed = False
        self._on_closed = on_closed
        self._stamp_task: asyncio.Task | None = None

    async def handle_offer(self, sdp: str) -> str:
        """Negotiate against the browser's offer; returns the answer SDP."""
        pc = RTCPeerConnection()
        self._pc = pc

        @pc.on("connectionstatechange")
        async def _on_state():
            logger.info(f"WebRTC connection state: {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                await self.close()

        # Track + codec preferences must be in place BEFORE the remote offer
        # is applied: aiortc negotiates the codec list (using the
        # transceiver's preferences) inside setRemoteDescription, and the
        # offer's video m-line pairs with this pre-created transceiver.
        sender = pc.addTrack(CaptureVideoTrack(self._capture, self._fps))
        self._prefer_h264(pc, sender)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        self._stamp_task = asyncio.create_task(self._stamp_encoder(sender))
        # aiortc completes ICE gathering before returning, so the SDP
        # already contains the host candidates (non-trickle signaling).
        return pc.localDescription.sdp

    async def _stamp_encoder(self, sender):
        """Apply this session's bitrate bounds to its (lazy) encoder.

        The sender creates its encoder on the first encoded frame, so poll
        for it briefly and then stamp the per-instance bounds that the
        patched ``target_bitrate`` setter honours, plus the starting rate.
        Until the stamp lands, the encoder runs at the module DEFAULT set
        in ``_set_global_bitrate_bounds`` — a few frames at most.
        """
        for _ in range(600):  # up to ~30 s (ICE + first frame can be slow)
            if self._closed:
                return
            enc = getattr(sender, "_RTCRtpSender__encoder", None)
            if enc is not None and hasattr(enc, "target_bitrate"):
                if self._min_bitrate is not None:
                    enc._kvm_min_bitrate = self._min_bitrate
                enc._kvm_max_bitrate = self._cap_bitrate
                enc.target_bitrate = self._start_bitrate
                logger.info(
                    "WebRTC encoder bounds: start "
                    f"{self._start_bitrate // 1000} kbps, "
                    f"floor {(self._min_bitrate or 0) // 1000} kbps, "
                    f"cap {self._cap_bitrate // 1000} kbps")
                return
            await asyncio.sleep(0.05)

    @staticmethod
    def _prefer_h264(pc: RTCPeerConnection, sender):
        """Put H.264 first in codec preferences, keeping VP8 as fallback."""
        if not _h264_available():
            return
        codecs = RTCRtpSender.getCapabilities("video").codecs
        h264 = [c for c in codecs if c.mimeType.lower() == "video/h264"]
        rest = [c for c in codecs if c.mimeType.lower() != "video/h264"]
        if not h264:
            return
        for transceiver in pc.getTransceivers():
            if transceiver.sender == sender:
                transceiver.setCodecPreferences(h264 + rest)
                break

    async def close(self):
        """Close the peer connection (idempotent) and fire on_closed."""
        if self._closed:
            return
        self._closed = True
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:
                pass
        cb, self._on_closed = self._on_closed, None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
