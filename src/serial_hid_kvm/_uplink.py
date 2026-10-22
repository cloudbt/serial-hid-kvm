"""Browser microphone uplink: play a viewer's microphone on a host output device.

The viewer captures its microphone (getUserMedia, echo cancellation and noise
suppression on) and sends mono int16 PCM over the viewer WebSocket as binary
frames::

    0x12 | u32 BE sample rate | int16 LE mono PCM

The server resamples to the output device's rate and plays it through a small
jitter buffer (sounddevice, WASAPI preferred).  In the TargetPC1 setup the
output is the host's 3.5 mm headphone jack wired into the target's microphone
input, so the phone's voice reaches the meeting on the target.

Only one viewer may send at a time; ownership lives in the web viewer.
"""

import collections
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ._captions import Resampler

logger = logging.getLogger(__name__)

TAG = 0x12
HEADER = 5                    # tag + u32 sample rate
MIN_RATE, MAX_RATE = 8000, 192000
MAX_FRAME_SAMPLES = 48000     # 1 s at 48 kHz; real frames are 20 ms
BLOCK_SECONDS = 0.010         # device callback block
PREBUFFER_SECONDS = 0.040     # start (and restart after an underrun) at 40 ms
MAX_BUFFER_SECONDS = 0.150    # beyond this the oldest audio is dropped


def parse_frame(message) -> tuple[int, bytes] | None:
    """(sample_rate, pcm) of an uplink frame, or None for anything else.

    Rejects other tags, implausible rates, odd-length PCM (int16 samples)
    and oversized frames, so a malformed frame can never reach the device.
    """
    if not isinstance(message, (bytes, bytearray, memoryview)):
        return None
    data = bytes(message)
    if len(data) <= HEADER or data[0] != TAG:
        return None
    rate = int.from_bytes(data[1:HEADER], "big")
    pcm = data[HEADER:]
    if not MIN_RATE <= rate <= MAX_RATE or len(pcm) % 2:
        return None
    if len(pcm) // 2 > MAX_FRAME_SAMPLES:
        return None
    return rate, pcm


def find_output_device(name: str, devices=None, hostapis=None):
    """Index of the first output device whose name contains *name*.

    Case-insensitive substring match; WASAPI is preferred (full, stable
    names and the OS mix format), other host APIs are a fallback.  A plain
    number selects that device index directly.  Returns None when nothing
    matches.  *devices* / *hostapis* default to sounddevice's lists.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None
    if devices is None or hostapis is None:
        import sounddevice as sd  # type: ignore[import-untyped]
        devices = sd.query_devices() if devices is None else devices
        hostapis = sd.query_hostapis() if hostapis is None else hostapis
    devices = list(devices)
    if wanted.isdigit():
        index = int(wanted)
        if index < len(devices) and devices[index]["max_output_channels"] > 0:
            return index
        return None
    wanted = wanted.casefold()
    for preferred in ("Windows WASAPI", None):
        for index, device in enumerate(devices):
            if (device["max_output_channels"] > 0
                    and wanted in device["name"].casefold()
                    and (preferred is None
                         or hostapis[device["hostapi"]]["name"] == preferred)):
                return index
    return None


def _init_com():
    """Executor initializer: COM for this thread (Windows).

    PortAudio's WASAPI streams fail with "Unanticipated host error ...
    WdmSyncIoctl" when opened on a thread without COM once another thread
    has opened a stream (e.g. the HDMI capture opened at startup).
    """
    if os.name == "nt":
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED


class JitterBuffer:
    """Thread-safe int16 FIFO with prebuffering and a latency cap.

    ``read`` returns silence until ``prebuffer`` samples are queued (at
    start and after every underrun), so playback never stutters sample by
    sample; ``push`` drops the oldest samples above ``limit`` so latency
    cannot grow when the browser sends slightly faster than the device
    plays.
    """

    def __init__(self, prebuffer: int, limit: int):
        self._prebuffer = max(1, prebuffer)
        self._limit = max(self._prebuffer, limit)
        self._chunks: collections.deque = collections.deque()
        self._size = 0
        self._playing = False
        self._lock = threading.Lock()
        self.dropped = 0
        self.underruns = 0

    def __len__(self):
        return self._size

    def push(self, samples: np.ndarray):
        if not len(samples):
            return
        with self._lock:
            self._chunks.append(samples)
            self._size += len(samples)
            excess = self._size - self._limit
            while excess > 0 and self._chunks:
                head = self._chunks[0]
                if len(head) <= excess:
                    self._chunks.popleft()
                    cut = len(head)
                else:
                    self._chunks[0] = head[excess:]
                    cut = excess
                self._size -= cut
                self.dropped += cut
                excess -= cut

    def read(self, count: int) -> np.ndarray:
        out = np.zeros(count, dtype=np.int16)
        with self._lock:
            if not self._playing:
                if self._size < self._prebuffer:
                    return out
                self._playing = True
            filled = 0
            while filled < count and self._chunks:
                head = self._chunks[0]
                take = min(count - filled, len(head))
                out[filled:filled + take] = head[:take]
                if take == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[take:]
                filled += take
                self._size -= take
            if filled < count:
                self._playing = False
                self.underruns += 1
        return out

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._size = 0
            self._playing = False


class UplinkPlayer:
    """Play uplink PCM on one output device; ``open``/``close`` per session.

    The device is held only while a viewer is sending, so it stays free
    (and silent) otherwise.  ``feed`` is cheap and called on the event loop;
    the device callback runs on PortAudio's thread.  Run ``open``/``close``
    on ``executor``: one thread with COM initialised (see ``_init_com``),
    so every device call happens where WASAPI can open streams.
    """

    def __init__(self, device_name: str):
        self.device_name = (device_name or "").strip()
        self.device_label = ""
        self.samplerate = 0
        self._channels = 0
        self._stream = None
        self._buffer: JitterBuffer | None = None
        self._resampler: Resampler | None = None
        self._lock = threading.Lock()
        self.executor = ThreadPoolExecutor(1, thread_name_prefix="mic-uplink",
                                           initializer=_init_com)

    @property
    def active(self) -> bool:
        return self._stream is not None

    def open(self):
        """Open the output stream (blocking: run it on ``executor``)."""
        import sounddevice as sd  # type: ignore[import-untyped]
        with self._lock:
            if self._stream is not None:
                return
            index = find_output_device(self.device_name)
            if index is None:
                raise OSError(f"no output device named like {self.device_name!r}")
            info = sd.query_devices(index)
            rate = int(info["default_samplerate"])
            channels = min(2, int(info["max_output_channels"]))
            self._buffer = JitterBuffer(int(rate * PREBUFFER_SECONDS),
                                        int(rate * MAX_BUFFER_SECONDS))
            self._resampler = None
            stream = sd.OutputStream(
                device=index, samplerate=rate, channels=channels, dtype="int16",
                blocksize=int(rate * BLOCK_SECONDS), latency="low",
                callback=self._callback)
            stream.start()
            self._stream = stream
            self.samplerate, self._channels = rate, channels
            self.device_label = info["name"]
        logger.info("Mic uplink playing on %s (%d Hz, %d ch)",
                    self.device_label, rate, channels)

    def close(self):
        """Stop playback and release the device (blocking: run it on ``executor``)."""
        with self._lock:
            stream, self._stream = self._stream, None
            buffer = self._buffer
            self._resampler = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            logger.warning("Mic uplink close failed: %s", exc)
        if buffer is not None:
            logger.info("Mic uplink stopped (underruns=%d, dropped=%d samples)",
                        buffer.underruns, buffer.dropped)

    def feed(self, rate: int, pcm: bytes):
        """Queue one browser frame (any rate, mono int16)."""
        buffer = self._buffer
        if self._stream is None or buffer is None:
            return
        if rate == self.samplerate:
            buffer.push(np.frombuffer(pcm, dtype="<i2").astype(np.int16))
            return
        resampler = self._resampler
        if resampler is None or resampler.in_rate != rate:
            resampler = self._resampler = Resampler(rate, 1, self.samplerate)
        buffer.push(resampler.process(pcm).astype(np.int16))

    def _callback(self, outdata, frames, time_info, status):
        buffer = self._buffer
        if buffer is None:
            outdata.fill(0)
            return
        samples = buffer.read(frames)
        outdata[:] = samples.reshape(-1, 1)
