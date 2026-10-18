"""Live captions: target HDMI audio -> kvm-ai-hub (LAN) -> caption text.

One CaptionService per KVM process (= per target).  It subscribes to the
server-side AudioCapture (the target's HDMI audio, never the host mic),
downmixes to mono 16 kHz s16le and streams ~100 ms PCM blocks to the hub's
``/v1/captions`` WebSocket.  The hub does AGC + VAD sentence splitting and
answers with final caption lines.

Reference counted: the hub connection exists only while at least one viewer
has CC on (plus a short linger so a page reload does not churn the hub).
Lost connections are retried with exponential backoff.  Captions are fanned
out to every subscribed viewer as plain text (WAN-friendly), and — while a
recording session is open — appended to ``<recording>.live.vtt`` aligned to
the recording start (reference only; the post-recording transcript is the
authoritative one).
"""
import asyncio
from collections import deque
import json
import logging
import math
from pathlib import Path
import queue
import time
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import websockets

logger = logging.getLogger(__name__)

HUB_RATE = 16000
BLOCK_SAMPLES = HUB_RATE // 10  # 100 ms per binary frame


class Resampler:
    """Stateful int16 interleaved PCM -> mono float -> ``out_rate`` int16.

    Anti-alias low-pass (windowed sinc at 0.45 x out_rate) followed by
    fractional-position linear interpolation; filter history and read
    position carry across blocks, so arbitrary block sizes give the same
    output as one large block.
    """

    def __init__(self, in_rate, channels, out_rate=HUB_RATE, taps=63):
        if in_rate <= 0 or channels <= 0:
            raise ValueError("Invalid audio format")
        self.in_rate, self.channels, self.out_rate = in_rate, channels, out_rate
        self.step = in_rate / out_rate
        if in_rate > out_rate:
            cutoff = 0.45 * out_rate / in_rate
            n = np.arange(taps) - (taps - 1) / 2
            h = np.sinc(2 * cutoff * n) * np.hamming(taps)
            self.h = h / h.sum()
        else:
            self.h = np.ones(1)
        self.history = np.zeros(len(self.h) - 1)
        self.prev = 0.0
        self.pos = 1.0  # read position; index 0 is the previous block's last sample

    def process(self, data):
        x = np.frombuffer(data, dtype="<i2")
        frames = len(x) // self.channels
        if not frames:
            return np.zeros(0, dtype="<i2")
        x = x[:frames * self.channels].reshape(frames, self.channels).astype(np.float64).mean(axis=1)
        if len(self.h) > 1:
            buf = np.concatenate([self.history, x])
            y = np.convolve(buf, self.h, mode="valid")
            self.history = buf[-(len(self.h) - 1):]
        else:
            y = x
        z = np.concatenate([[self.prev], y])
        last = len(z) - 1
        count = int(math.floor((last - self.pos) / self.step)) + 1 if last >= self.pos else 0
        if count:
            idx = self.pos + np.arange(count) * self.step
            out = np.interp(idx, np.arange(len(z)), z)
            self.pos = idx[-1] + self.step - last
        else:
            out = np.zeros(0)
            self.pos -= last
        self.prev = z[-1]
        return np.clip(np.round(out), -32768, 32767).astype("<i2")


def captions_ws_url(base):
    """http(s)://host:port[/prefix] -> ws(s)://host:port[/prefix]/v1/captions."""
    parts = urlsplit(base.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        raise ValueError("ai_hub_url must be http(s)://host:port")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/v1/captions", "", ""))


def _vtt_time(seconds):
    ms = int(round(max(0.0, seconds) * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


class LiveVtt:
    """Append-only WebVTT aligned to a recording origin (epoch seconds)."""

    def __init__(self, path, origin):
        self.path = Path(path)
        self.origin = float(origin)
        self.created = time.time()
        self.count = 0

    def add(self, start_epoch, end_epoch, text):
        start, end = start_epoch - self.origin, end_epoch - self.origin
        text = " ".join(str(text).split()).replace("-->", "->")
        if end <= 0 or not text:
            return  # spoken before the recording started
        start = max(0.0, start)
        end = max(end, start + 0.5)
        head = "" if self.path.exists() else (
            "WEBVTT\n\nNOTE Live captions (kvm-ai-hub). Reference only; "
            "timings are approximate. The post-recording transcript is authoritative.\n\n")
        self.count += 1
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(f"{head}{self.count}\n{_vtt_time(start)} --> {_vtt_time(end)}\n{text}\n\n")


class HubError(RuntimeError):
    pass


def _get_chunk(q):
    try:
        return q.get(timeout=0.2)
    except queue.Empty:
        return None


class CaptionService:
    LINGER = 5.0
    MAX_BACKOFF = 30.0
    HISTORY = 300
    RECORDING_MAX_AGE = 8 * 3600

    def __init__(self, audio, hub_url, key=None, language="auto", enabled=True):
        self._audio = audio
        self._key = key or None
        self._language = language or "auto"
        self.reason = ""
        try:
            self._url = captions_ws_url(hub_url or "")
        except ValueError as exc:
            self._url, self.reason = None, str(exc)
        if not enabled:
            self.reason = "Captions disabled by server configuration"
        elif audio is None:
            self.reason = "No target HDMI audio device configured on the server"
        self.available = bool(enabled and audio is not None and self._url)
        self._subscribers = set()
        self._idle_since = None
        self._task = None
        self._kick = None
        self._state = ("off", "")
        self._epoch = None
        self._history = deque(maxlen=self.HISTORY)
        self._recordings = {}
        # Optional hooks (LiveMeeting): on_final(item) for every final caption,
        # on_subscribe() -> extra messages queued for a newly subscribed viewer.
        self.on_final = None
        self.on_subscribe = None

    @property
    def active(self):
        """True while any viewer has CC on (or within the linger window)."""
        return self._wanted()

    def broadcast(self, message):
        self._broadcast(message)

    def clear_history(self):
        self._history.clear()

    # -- viewers -----------------------------------------------------------
    def state_message(self):
        state, detail = self._state
        return {"type": "captions_state", "state": state, "detail": detail}

    def subscribe(self):
        """Register a viewer queue; replays recent captions, starts the hub link."""
        if not self.available:
            raise ValueError(self.reason or "Captions unavailable")
        q = asyncio.Queue(maxsize=500)
        q.put_nowait(self.state_message())
        for item in list(self._history)[-20:]:
            q.put_nowait({**item, "replay": True})
        self._subscribers.add(q)
        self._idle_since = None
        if self.on_subscribe is not None:
            try:
                for message in self.on_subscribe() or ():
                    q.put_nowait(message)
            except Exception:
                logger.exception("Caption subscribe hook failed")
        if self._task is None or self._task.done():
            self._kick = asyncio.Event()
            self._task = asyncio.create_task(self._run())
        elif self._kick is not None:
            self._kick.set()  # skip a pending backoff wait
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)
        if not self._subscribers and self._idle_since is None:
            self._idle_since = time.monotonic()

    def _broadcast(self, message):
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # slow viewer: drop its oldest line
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(message)

    def _set_state(self, state, detail=""):
        if self._key:
            detail = detail.replace(self._key, "***")
        if (state, detail) == self._state:
            return
        self._state = (state, detail)
        logger.info("Captions %s%s", state, f": {detail}" if detail else "")
        self._broadcast(self.state_message())

    def _wanted(self):
        if self._subscribers:
            return True
        return self._idle_since is not None and time.monotonic() - self._idle_since < self.LINGER

    # -- recordings --------------------------------------------------------
    def begin_recording(self, session, path, origin):
        if session not in self._recordings:
            self._recordings[session] = LiveVtt(path, origin)
        cutoff = time.time() - self.RECORDING_MAX_AGE
        for key in [k for k, v in self._recordings.items() if v.created < cutoff]:
            del self._recordings[key]  # abandoned (never finished) sessions

    def end_recording(self, session):
        vtt = self._recordings.pop(session, None)
        return vtt.path if vtt is not None and vtt.path.exists() else None

    # -- hub link ----------------------------------------------------------
    async def close(self):
        self._subscribers.clear()
        self._idle_since = time.monotonic() - self.LINGER
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def _describe(self, exc):
        status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
        if status in (401, 403):
            return f"hub rejected the key (HTTP {status}); check SHKVM_AI_HUB_KEY"
        if status:
            return f"hub refused captions (HTTP {status})"
        if isinstance(exc, HubError):
            return str(exc)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "hub did not answer in time"
        if isinstance(exc, websockets.ConnectionClosed):
            return "hub connection closed"
        if isinstance(exc, OSError):
            return f"hub unreachable ({urlsplit(self._url).netloc})"
        return f"hub error: {type(exc).__name__}"

    async def _run(self):
        backoff = 1.0
        try:
            while self._wanted():
                self._set_state("connecting")
                try:
                    await self._session()
                    backoff = 1.0
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._wanted():
                        break
                    self._set_state("offline", self._describe(exc))
                self._kick.clear()
                deadline = time.monotonic() + backoff
                while self._wanted() and time.monotonic() < deadline and not self._kick.is_set():
                    try:
                        await asyncio.wait_for(self._kick.wait(), min(1.0, max(0.05, deadline - time.monotonic())))
                    except asyncio.TimeoutError:
                        pass
                backoff = min(backoff * 2, self.MAX_BACKOFF)
        finally:
            self._epoch = None
            self._set_state("off")

    def _connect(self):
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        options = dict(open_timeout=10, ping_interval=20, ping_timeout=20,
                       max_size=1024 * 1024, compression=None)
        try:
            from websockets.asyncio.client import connect
            return connect(self._url, additional_headers=headers, **options)
        except ImportError:  # websockets < 14
            return websockets.connect(self._url, extra_headers=headers, **options)

    async def _session(self):
        loop = asyncio.get_running_loop()
        async with self._connect() as ws:
            await ws.send(json.dumps({"type": "start", "sample_rate": HUB_RATE, "language": self._language}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply.get("type") == "ready":
                    break
                if reply.get("type") == "error":
                    raise HubError("hub: " + str(reply.get("message", "error"))[:200])
            q = self._audio.subscribe()
            receiver = None
            try:
                resampler = Resampler(self._audio.samplerate, self._audio.channels)
                pending = np.zeros(0, dtype="<i2")
                self._epoch = None
                receiver = asyncio.create_task(self._receive(ws))
                self._set_state("live")
                while self._wanted():
                    chunk = await loop.run_in_executor(None, _get_chunk, q)
                    if receiver.done():
                        receiver.result()
                        raise HubError("hub closed the caption stream")
                    if chunk is None:
                        continue
                    if self._epoch is None:
                        frames = len(chunk) // (2 * self._audio.channels)
                        self._epoch = time.time() - frames / self._audio.samplerate
                    pending = np.concatenate([pending, resampler.process(chunk)])
                    while len(pending) >= BLOCK_SAMPLES:
                        await ws.send(pending[:BLOCK_SAMPLES].tobytes())
                        pending = pending[BLOCK_SAMPLES:]
                await ws.send(json.dumps({"type": "stop"}))
                try:  # let the hub flush the sentence in progress
                    await asyncio.wait_for(asyncio.shield(receiver), 3)
                except (asyncio.TimeoutError, websockets.ConnectionClosed, HubError):
                    pass
            finally:
                self._audio.unsubscribe(q)
                if receiver is not None and not receiver.done():
                    receiver.cancel()

    async def _receive(self, ws):
        async for raw in ws:
            if not isinstance(raw, str):
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "error":
                raise HubError("hub: " + str(msg.get("message", "error"))[:200])
            if kind == "caption":
                self._caption(msg)

    def _caption(self, msg):
        text = str(msg.get("text", "")).strip()[:2000]
        try:
            t0, t1 = float(msg.get("t0", 0)), float(msg.get("t1", 0))
        except (TypeError, ValueError):
            return
        if not text or not math.isfinite(t0) or not math.isfinite(t1):
            return
        epoch = self._epoch or time.time()
        item = {"type": "caption", "seq": msg.get("seq"), "t0": t0, "t1": t1, "text": text,
                "lang": str(msg.get("lang") or "")[:16], "final": msg.get("final", True) is not False,
                "at": int((epoch + t0) * 1000), "end": int((epoch + max(t0, t1)) * 1000)}
        # Interim (final: false) results only update viewers' overlays; the
        # hub re-sends the same seq with final: true when the sentence ends.
        if item["final"]:
            self._history.append(item)
            for vtt in list(self._recordings.values()):
                try:
                    vtt.add(epoch + t0, epoch + max(t1, t0), text)
                except OSError as exc:
                    logger.warning("Live VTT write failed (%s): %s", vtt.path, exc)
            if self.on_final is not None:
                try:
                    self.on_final(item)
                except Exception:
                    logger.exception("Caption final hook failed")
        self._broadcast(item)
