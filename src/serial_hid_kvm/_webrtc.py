"""WebRTC (H.264) low-latency video streaming for the web viewer.

The browser receives a native ``<video>`` MediaStream (hardware-decoded
H.264 with WebRTC pacing/jitter handling) while the *server* keeps
ownership of the capture device — unlike Direct mode, which hands the
device to the browser.  Server-side consumers (API ``capture_frame``,
OCR, MCP) therefore keep working while the viewer streams.

Signaling rides on the existing web-viewer WebSocket (``webrtc_offer`` /
``webrtc_answer`` / ``webrtc_stop`` JSON messages, see _web_viewer.py).
Everything runs on 127.0.0.1 host candidates — no STUN/TURN needed.

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


def _raise_encoder_bitrate_caps(bitrate: int):
    """Lift aiortc's built-in video encoder bitrate caps to *bitrate*.

    aiortc clamps H.264 to 3 Mbps and VP8 to 1.5 Mbps, far too low for
    crisp 1080p60 desktop content on a loopback/LAN link.  The caps are
    module-level constants read at runtime by the ``target_bitrate``
    setter (which REMB feedback drives), so raising them here both starts
    the encoder at *bitrate* and lets congestion feedback stay there.
    """
    from aiortc.codecs import h264, vpx
    for mod in (h264, vpx):
        mod.DEFAULT_BITRATE = bitrate
        mod.MAX_BITRATE = max(bitrate, mod.MAX_BITRATE)
        # Floor the rate controller as well: transient REMB dips from the
        # browser would otherwise crater quality (visible as blur during
        # scrolling), and every >10% target change rebuilds the encoder.
        mod.MIN_BITRATE = max(mod.MIN_BITRATE, bitrate // 2)


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
    """One peer connection streaming the capture to one viewer client."""

    def __init__(self, capture, fps: int, bitrate: int, on_closed=None):
        _raise_encoder_bitrate_caps(bitrate)
        self._capture = capture
        self._fps = fps
        self._pc: RTCPeerConnection | None = None
        self._closed = False
        self._on_closed = on_closed

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
        # aiortc completes ICE gathering before returning, so the SDP
        # already contains the host candidates (non-trickle signaling).
        return pc.localDescription.sdp

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
