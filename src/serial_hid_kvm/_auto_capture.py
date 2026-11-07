"""Automatic meeting capture: Target HDMI sound -> audio recording + live captions.

One ``AutoCapture`` per Target process.  A monitor thread reads the server's
HDMI ``AudioCapture`` (the same feed the viewers and the captions use) and
feeds ``SoundDetector``: 3 s of sound within 10 s starts a *capture session*,
3 min of silence ends it.  The HDMI digital silence floor is about -84 dBFS,
so a plain level threshold is reliable; the host microphone is never a trigger.

A capture session records audio only (no video) into
``meetings/audio/<YYYY-MM-DD>/<YYYY-MM-DD_HHMMSS>.mka``: Opus tracks
混合 (both), Target HDMI (对方) and the host microphone (我), with the last
15 s before the trigger included.  It also switches CC on (when it was off)
and back off at the end; nothing calls AI.

Modes (server-wide, persisted in ``meetings/.capture.json``):

- ``auto``: sound starts and silence ends a session.  Stopping a running
  session by hand skips the rest of this meeting (until the next silence).
- ``manual``: only the meeting button starts and stops a session.  A
  manual session stops by itself only after ``MANUAL_SILENCE`` of silence.
- ``off``: nothing is recorded.

When several Targets hold meetings at once, only one of them captions the
host microphone (``Presence``): the Target the user last operated, else the
one whose meeting started first.  Every session still records the microphone
track.  Presence files live in ``%LOCALAPPDATA%/serial-hid-kvm/presence``.

If the hub was offline during a session, the missed HDMI stretches are
transcribed from the recording afterwards (``Backfill``); the rows go to
``meetings/.backfill/<audio stem>.jsonl`` and the daily Markdown shows them.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ._server_recorder import BLOCK, SAMPLE_RATE, AudioBuffer, HdmiFeed, MicFeed, find_input_device

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
MODES = ("auto", "manual", "off")
PREROLL_SECONDS = 15.0
MANUAL_SILENCE = 30 * 60
MIN_FREE_BYTES = 2 * 1024 ** 3
PRESENCE_STALE = 15.0
BACKFILL_MARGIN = 2.0
BACKFILL_RETRY = 60.0
BACKFILL_GIVE_UP = 24 * 3600


def rms_dbfs(pcm: bytes) -> float:
    """RMS level of int16 PCM in dBFS (-120 for silence)."""
    x = np.frombuffer(pcm, dtype="<i2")
    if not len(x):
        return -120.0
    rms = math.sqrt(float(np.mean(np.square(x.astype(np.float64))))) / 32768.0
    return 20 * math.log10(rms) if rms > 1e-6 else -120.0


def meetings_root(config) -> Path:
    """``meetings_dir`` or, by default, ``meetings`` beside the recordings folder."""
    explicit = (getattr(config, "meetings_dir", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    recordings = Path(config.recording_dir).expanduser()
    return (recordings.parent if recordings.name.lower() == "recordings" else recordings) / "meetings"


def shared_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "serial-hid-kvm"


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Sound detection
# ---------------------------------------------------------------------------

class SoundDetector:
    """Sound activity with hysteresis: start after ``need`` s of sound within
    ``window`` s, stop after ``silence`` s without any.  ``feed`` returns
    "start", "stop" or None."""

    def __init__(self, threshold_db=-55.0, window=10.0, need=3.0, silence=180.0):
        self.threshold_db = float(threshold_db)
        self.window, self.need, self.silence = float(window), float(need), float(silence)
        self.active = False
        self.last_loud = None
        self._recent = collections.deque()   # (time, seconds) of loud blocks
        self._loud = 0.0

    def feed(self, db, seconds, now):
        loud = db >= self.threshold_db
        if loud:
            self.last_loud = now
            self._recent.append((now, seconds))
            self._loud += seconds
        while self._recent and now - self._recent[0][0] > self.window:
            self._loud -= self._recent.popleft()[1]
        if not self.active:
            if self._loud >= self.need - 1e-9:
                self.active = True
                return "start"
        elif self.last_loud is not None and now - self.last_loud >= self.silence:
            self.active = False
            self._recent.clear()
            self._loud = 0.0
            return "stop"
        return None

    def silent_for(self, now):
        return float("inf") if self.last_loud is None else now - self.last_loud


# ---------------------------------------------------------------------------
# Audio file writer (ffmpeg, Opus in Matroska: readable after a crash)
# ---------------------------------------------------------------------------

_ENCODER = None


def audio_encoder():
    """libopus when ffmpeg has it, else AAC (cached)."""
    global _ENCODER
    if _ENCODER is None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found")
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _ENCODER = (ffmpeg, "libopus" if " libopus " in out else "aac")
    return _ENCODER


def ffmpeg_audio_args(ffmpeg, encoder, path):
    """stdin: s16le 48 kHz, channel 0 = Target HDMI (mono), channel 1 = host mic."""
    bitrate = "32k" if encoder == "libopus" else "64k"
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
            "-filter_complex", "[0:a]pan=mono|c0=c0+c1[mix];[0:a]pan=mono|c0=c0[hdmi];[0:a]pan=mono|c0=c1[mic]",
            "-map", "[mix]", "-map", "[hdmi]", "-map", "[mic]",
            "-c:a", encoder, "-b:a", bitrate,
            "-metadata:s:a:0", "title=混合", "-metadata:s:a:1", "title=Target HDMI（对方）",
            "-metadata:s:a:2", "title=本机麦克风（我）",
            "-f", "matroska", str(path)]


def _mono(block: np.ndarray) -> np.ndarray:
    """(n, 2) int16 -> (n,) int16."""
    return (block.astype(np.int32).sum(axis=1) // 2).astype(np.int16)


class AudioSession:
    """One capture session's recording: HDMI + host mic -> .mka via ffmpeg."""

    def __init__(self, path: Path, hdmi_capture, mic_name: str, preroll=(), mic_opener=None,
                 min_free_bytes=MIN_FREE_BYTES, on_fail=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg, encoder = audio_encoder()
        self.log = self.path.with_suffix(".ffmpeg.log")
        self._log_file = self.log.open("wb")
        self.proc = subprocess.Popen(ffmpeg_audio_args(ffmpeg, encoder, self.path), stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=self._log_file,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.min_free_bytes = min_free_bytes
        self.on_fail = on_fail
        self.warnings = []
        self.stopped_reason = None
        self._stop = threading.Event()
        self._hdmi_buf, self._mic_buf = AudioBuffer(), AudioBuffer()
        self._feeds = [HdmiFeed(hdmi_capture, self._hdmi_buf)]
        self.mic = ""
        device = find_input_device(mic_name) if mic_name else None
        if mic_name and device is None:
            self.warnings.append(f"本机麦克风「{mic_name}」没有找到，只录 Target 的声音")
        elif device is not None:
            try:
                feed = (mic_opener or MicFeed)(device, self._mic_buf)
                self._feeds.append(feed)
                self.mic = getattr(feed, "name", mic_name)
            except Exception as exc:
                self.warnings.append(f"本机麦克风打不开：{exc}")
        for warning in self.warnings:
            logger.warning("Capture audio: %s", warning)
        self._preroll = self._preroll_pcm(preroll, hdmi_capture)
        self._thread = threading.Thread(target=self._run, name="capture-audio", daemon=True)
        self._thread.start()

    @staticmethod
    def _preroll_pcm(chunks, capture):
        from ._server_recorder import to_block_format
        if not chunks:
            return b""
        data = to_block_format(np.frombuffer(b"".join(chunks), dtype=np.int16),
                               capture.samplerate, capture.channels)
        mono = _mono(data)
        return np.stack([mono, np.zeros_like(mono)], axis=1).astype(np.int16).tobytes()

    def _run(self):
        try:
            if self._preroll:
                self.proc.stdin.write(self._preroll)
            t0 = time.monotonic()
            samples = 0
            next_disk = t0
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_disk:
                    next_disk = now + 10
                    if shutil.disk_usage(self.path.parent).free < self.min_free_bytes:
                        self.stopped_reason = "disk_full"
                        break
                due = (now - t0) * SAMPLE_RATE
                while samples + BLOCK <= due:
                    block = np.stack([_mono(self._hdmi_buf.read()), _mono(self._mic_buf.read())], axis=1)
                    self.proc.stdin.write(block.astype(np.int16).tobytes())
                    samples += BLOCK
                self._stop.wait(0.02)
        except OSError as exc:
            self.stopped_reason = "error"
            self.warnings.append(f"ffmpeg 停止接收音频：{self.error() or exc}")
            logger.warning("Capture audio failed: %s", self.warnings[-1])
        if self.stopped_reason and self.on_fail:
            try:
                self.on_fail(self.stopped_reason)
            except Exception:
                logger.exception("Capture audio fail callback")

    def error(self):
        try:
            self._log_file.flush()
            return self.log.read_text(encoding="utf-8", errors="replace").strip()[-300:]
        except (OSError, ValueError):
            return ""

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        for feed in self._feeds:
            try:
                feed.close()
            except Exception:
                pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        self._log_file.close()
        if code == 0 and not self.error():
            self.log.unlink(missing_ok=True)
        elif code:
            self.warnings.append(f"ffmpeg exited with {code}: {self.error()}")
        return code


# ---------------------------------------------------------------------------
# Presence: which Target captions the host microphone
# ---------------------------------------------------------------------------

class Presence:
    """Shared files under ``<shared>/presence``: one heartbeat per Target and
    ``focus.json`` (the Target the user operated last)."""

    def __init__(self, target, folder=None):
        self.target = target
        self.folder = Path(folder) if folder else shared_dir() / "presence"
        self._focus_written = 0.0
        self._focus = None

    def beat(self, meeting, since):
        _write_json(self.folder / f"{self.target}.json",
                    {"target": self.target, "pid": os.getpid(), "at": time.time(),
                     "meeting": bool(meeting), "since": since})

    def leave(self):
        try:
            (self.folder / f"{self.target}.json").unlink()
        except OSError:
            pass

    def touch_focus(self, now=None, force=False):
        """The user operated this Target: it becomes the focus (rate-limited unless forced)."""
        now = time.time() if now is None else now
        if not force and self._focus == self.target and now - self._focus_written < 30:
            return False
        _write_json(self.folder / "focus.json", {"target": self.target, "at": now})
        self._focus, self._focus_written = self.target, now
        return True

    def live(self, now=None):
        now = time.time() if now is None else now
        found = []
        for path in self.folder.glob("*.json"):
            if path.name == "focus.json":
                continue
            data = _read_json(path)
            if data and data.get("meeting") and now - float(data.get("at") or 0) < PRESENCE_STALE:
                found.append(data)
        return found

    def focus(self):
        data = _read_json(self.folder / "focus.json")
        self._focus = data.get("target") if data else None
        return self._focus

    def mic_owner(self, now=None):
        """The Target that captions the microphone now (None: no meeting anywhere)."""
        live = self.live(now)
        names = {p["target"] for p in live}
        focus = self.focus()
        if focus in names:
            return focus
        if not live:
            return None
        return min(live, key=lambda p: (float(p.get("since") or 0), str(p["target"])))["target"]


# ---------------------------------------------------------------------------
# Backfill: transcribe the HDMI stretches the hub missed
# ---------------------------------------------------------------------------

class Backfill:
    """Pending jobs in ``meetings/.backfill/pending/*.json``; results as
    ``meetings/.backfill/<stem>.jsonl`` rows ``{at, end, text, speaker, source}``."""

    def __init__(self, root: Path, hub, language="auto", on_done=None):
        self.folder = Path(root) / ".backfill"
        self.hub = hub
        self.language = language
        self.on_done = on_done
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def enqueue(self, audio: Path, started: float, gaps):
        gaps = [(max(0.0, a - BACKFILL_MARGIN), b + BACKFILL_MARGIN) for a, b in gaps if b - a >= 3]
        if not gaps:
            return None
        job = {"audio": str(audio), "started": started, "gaps": gaps, "created": time.time(), "tries": 0}
        path = self.folder / "pending" / (Path(audio).stem + ".json")
        _write_json(path, job)
        logger.info("Backfill queued for %s: %d gap(s)", Path(audio).name, len(gaps))
        self._wake.set()
        return path

    def start(self):
        if self.hub is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="capture-backfill", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            for path in sorted((self.folder / "pending").glob("*.json")):
                if self._stop.is_set():
                    return
                self.process(path)
            self._wake.wait(BACKFILL_RETRY)
            self._wake.clear()

    def process(self, path: Path):
        job = _read_json(path)
        if not job:
            path.unlink(missing_ok=True)
            return False
        audio = Path(job["audio"])
        try:
            rows = []
            for a, b in job["gaps"]:
                rows += self._transcribe(audio, float(job["started"]), a, b)
        except Exception as exc:
            job["tries"] = job.get("tries", 0) + 1
            job["error"] = str(exc)[:300]
            if time.time() - job.get("created", 0) > BACKFILL_GIVE_UP:
                logger.warning("Backfill for %s given up: %s", audio.name, exc)
                path.rename(path.with_suffix(".failed"))
            else:
                _write_json(path, job)
                logger.info("Backfill for %s will retry: %s", audio.name, exc)
            return False
        out = self.folder / (audio.stem + ".jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8", newline="\n") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        path.unlink(missing_ok=True)
        logger.info("Backfill for %s done: %d line(s)", audio.name, len(rows))
        if self.on_done:
            self.on_done(job["started"])
        return True

    def _transcribe(self, audio: Path, started: float, a: float, b: float):
        from ._minutes import validate_segments
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found")
        flac = self.folder / "work" / f"{audio.stem}-{int(a)}.flac"
        flac.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}",
                        "-i", str(audio), "-map", "0:a:1", "-ac", "1", "-ar", "16000", str(flac)],
                       check=True, capture_output=True, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            job = self.hub.submit(flac, self.language)
            deadline = time.time() + 1800
            while True:
                state = self.hub.status(job)
                if state.get("status") == "done":
                    break
                if state.get("status") == "failed":
                    raise RuntimeError("hub 转写失败：" + str(state.get("error") or "")[:200])
                if time.time() > deadline or self._stop.wait(3):
                    raise RuntimeError("hub 转写超时")
            segments = validate_segments(self.hub.result(job))
            self.hub.delete(job)
        finally:
            flac.unlink(missing_ok=True)
        return [{"at": int((started + a + s["start"]) * 1000), "end": int((started + a + s["end"]) * 1000),
                 "text": s["text"], "lang": s.get("lang", ""), "speaker": "remote", "source": "batch"}
                for s in segments]


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------

class AutoCapture:
    """Capture sessions for one Target (see the module docstring)."""

    def __init__(self, audio, root, target, meeting=None, captions=None, *, mode="auto",
                 threshold_db=-55.0, silence=180.0, mic_name="EarPods", presence=None,
                 backfill=None, publish=None, on_day_changed=None, mic_opener=None):
        self.audio = audio
        self.root = Path(root)
        self.audio_dir = self.root / "audio"
        self.target = target or "target"
        self.meeting = meeting
        self.captions = captions
        self.mic_name = mic_name or ""
        self.presence = presence
        self.backfill = backfill
        self.publish = publish or (lambda message: None)
        self.on_day_changed = on_day_changed or (lambda day: None)
        self.mic_opener = mic_opener
        self.available = audio is not None
        self.reason = "" if self.available else "这台 Target 没有配置 HDMI 声音"
        saved = _read_json(self.root / ".capture.json") or {}
        self.mode = saved.get("mode") if saved.get("mode") in MODES else (mode if mode in MODES else "auto")
        self.detector = SoundDetector(threshold_db, silence=silence)
        self.level = -120.0
        self.snoozed = False
        self.session = None        # dict while recording
        self.notice = ""
        self.mic_owner = None
        self._recorder = None
        self._auto_cc = False
        self._gaps = []
        self._gap_open = None
        self._lock = threading.Lock()
        self._preroll = collections.deque()
        self._preroll_frames = 0
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._busy = False

    # -- lifecycle -----------------------------------------------------------
    def start(self, loop):
        self._loop = loop
        self._recover()
        if self.backfill is not None:
            self.backfill.start()
        if not self.available:
            return
        self._queue = self.audio.subscribe()
        self._thread = threading.Thread(target=self._monitor, name="capture-monitor", daemon=True)
        self._thread.start()

    async def close(self):
        self._stop.set()
        if self.session is not None:
            await self.stop("shutdown")
        if self._thread is not None:
            self._thread.join(timeout=2)
            self.audio.unsubscribe(self._queue)
        if self.backfill is not None:
            self.backfill.stop()
        if self.presence is not None:
            self.presence.leave()

    def _recover(self):
        """Sidecars left open by a crash: close them at the file's last write."""
        for side in self.audio_dir.glob("*/*.json"):
            data = _read_json(side)
            if data and data.get("end") is None:
                media = side.with_suffix(".mka")
                data["end"] = media.stat().st_mtime if media.exists() else data.get("start")
                data["reason"] = "recovered"
                _write_json(side, data)
                logger.warning("Capture session %s closed after a restart", side.stem)

    # -- monitor thread ------------------------------------------------------
    def _monitor(self):
        rate, channels = self.audio.samplerate, self.audio.channels
        limit = int(PREROLL_SECONDS * rate)
        while not self._stop.is_set():
            try:
                chunk = self._queue.get(timeout=0.5)
            except queue.Empty:
                chunk = None
            now = time.monotonic()
            if chunk is None:
                event = self.detector.feed(-120.0, 0.0, now)   # no data = silence (time still passes)
            else:
                frames = len(chunk) // (2 * channels)
                with self._lock:
                    self._preroll.append(chunk)
                    self._preroll_frames += frames
                    while self._preroll_frames - len(self._preroll[0]) // (2 * channels) >= limit:
                        self._preroll_frames -= len(self._preroll.popleft()) // (2 * channels)
                self.level = rms_dbfs(chunk)
                event = self.detector.feed(self.level, frames / rate, now)
            if event:
                self._call(self.sound_event, event)
            if (self.session is not None and self.session["trigger"] != "auto"
                    and self.detector.silent_for(now) >= MANUAL_SILENCE
                    and now - self.session["mono"] >= MANUAL_SILENCE):
                self._call(self._manual_timeout)

    def _call(self, fn, *args):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        import asyncio

        def done(task):
            if not task.cancelled() and task.exception() is not None:
                logger.error("Capture task failed: %s", task.exception())

        def run():
            result = fn(*args)
            if asyncio.iscoroutine(result):
                loop.create_task(result).add_done_callback(done)
        loop.call_soon_threadsafe(run)

    def preroll_chunks(self):
        with self._lock:
            return list(self._preroll)

    # -- events (event loop) -------------------------------------------------
    async def sound_event(self, event):
        if event == "start":
            if self.mode == "auto" and not self.snoozed and self.session is None:
                await self.begin("auto")
        elif event == "stop":
            self.snoozed = False
            if self.session is not None and self.session["trigger"] == "auto":
                await self.stop("silence")
            else:
                self.broadcast()

    async def _manual_timeout(self):
        if self.session is not None and self.session["trigger"] != "auto":
            await self.stop("silence")

    async def begin(self, trigger="manual"):
        """Start a capture session (no-op while one runs)."""
        import asyncio
        if not self.available:
            raise ValueError(self.reason)
        if self.session is not None or self._busy:
            return self.state()
        self._busy = True
        try:
            now = time.time()
            stamp = datetime.fromtimestamp(now, JST)
            path = self.audio_dir / stamp.strftime("%Y-%m-%d") / (stamp.strftime("%Y-%m-%d_%H%M%S") + ".mka")
            chunks = self.preroll_chunks() if trigger == "auto" else []
            pre = sum(len(c) for c in chunks) / (2 * self.audio.channels * self.audio.samplerate)
            recorder = await asyncio.to_thread(
                AudioSession, path, self.audio, self.mic_name, chunks, self.mic_opener,
                MIN_FREE_BYTES, lambda reason: self._call(self.stop, reason))
            started = now - pre
            self._recorder = recorder
            self.session = {"file": path, "start": started, "mono": time.monotonic(), "trigger": trigger}
            self._gaps, self._gap_open = [], None
            self.snoozed = False
            _write_json(path.with_suffix(".json"),
                        {"target": self.target, "file": path.name, "start": started, "end": None,
                         "trigger": trigger, "preroll": round(pre, 2), "mic": recorder.mic,
                         "warnings": recorder.warnings})
            self.notice = "；".join(recorder.warnings)
            logger.info("Capture session started (%s): %s", trigger, path.name)
            self._captions_on(chunks if trigger == "auto" else None)
            if self.captions is not None and self.captions.on and self.captions.remote_state() == "offline":
                self._gap_open = started   # the hub is already unreachable

            self.on_day_changed(stamp.date())
        finally:
            self._busy = False
        self.broadcast()
        return self.state()

    async def stop(self, reason="manual"):
        """End the running session; a manual stop in auto mode skips this meeting."""
        import asyncio
        session, recorder = self.session, self._recorder
        if session is None or self._busy:
            return self.state()
        self.session, self._recorder = None, None
        if reason == "manual" and self.mode == "auto" and self.detector.active:
            self.snoozed = True
        reason = recorder.stopped_reason or reason
        self._busy = True          # no new session until this file is closed
        try:
            await asyncio.to_thread(recorder.close)
            self._finish(session, recorder, reason)
        finally:
            self._busy = False
        self.broadcast()
        return self.state()

    def _finish(self, session, recorder, reason):
        end = time.time()
        side = session["file"].with_suffix(".json")
        data = _read_json(side) or {}
        data.update(end=end, reason=reason, warnings=recorder.warnings)
        _write_json(side, data)
        self._captions_off()
        if self._gap_open is not None:
            self._gaps.append((self._gap_open, end))
            self._gap_open = None
        if self.backfill is not None and self._gaps:
            gaps = [(a - session["start"], b - session["start"]) for a, b in self._gaps]
            self.backfill.enqueue(session["file"], session["start"], gaps)
        logger.info("Capture session stopped (%s): %s, %.0f s", reason, session["file"].name, end - session["start"])
        if reason in ("disk_full", "error"):
            self.notice = {"disk_full": "磁盘空间不足，录音已停止", "error": "录音出错，已停止"}[reason]
        self.on_day_changed(datetime.fromtimestamp(session["start"], JST).date())

    async def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("模式只能是 auto / manual / off")
        self.mode = mode
        _write_json(self.root / ".capture.json", {"mode": mode})
        self.snoozed = False
        logger.info("Capture mode: %s", mode)
        if mode == "off" and self.session is not None:
            await self.stop("mode_off")
        elif mode == "auto" and self.session is None and self.detector.active:
            await self.begin("auto")
        self.broadcast()

    # -- captions --------------------------------------------------------------
    def _captions_on(self, preroll):
        captions, meeting = self.captions, self.meeting
        if captions is None or meeting is None or not captions.available or captions.on:
            return
        if preroll:
            captions.set_preroll(preroll)
        try:
            meeting.set_cc(True, "auto capture", persist=False)
            self._auto_cc = True
        except ValueError as exc:
            captions.set_preroll(None)
            logger.warning("Capture: captions not started: %s", exc)

    def _captions_off(self):
        if self._auto_cc and self.captions is not None and self.captions.on:
            self.meeting.set_cc(False, "capture session ended", persist=False)
        self._auto_cc = False

    def cc_touched(self):
        """A viewer switched CC by hand: leave it as they set it at the end."""
        self._auto_cc = False

    def caption_state(self, speaker, state):
        """CaptionService link state hook: remember the HDMI stretches without captions."""
        if speaker != "remote" or self.session is None:
            return
        now = time.time()
        if state == "offline" and self._gap_open is None:
            self._gap_open = now
        elif state == "live" and self._gap_open is not None:
            self._gaps.append((self._gap_open, now))
            self._gap_open = None

    # -- presence --------------------------------------------------------------
    def touch_focus(self, force=False):
        if self.presence is None:
            return
        try:
            if self.presence.touch_focus(force=force) or force:
                self.tick_presence()
        except OSError as exc:
            logger.debug("Focus not written: %s", exc)

    def tick_presence(self):
        """Heartbeat + who captions the microphone (call every few seconds)."""
        if self.presence is None or self.captions is None:
            return
        meeting = bool(self.captions.on)
        try:
            self.presence.beat(meeting, self.captions.on_since)
            self.presence.focus()   # refresh the cache touch_focus compares with
            owner = self.presence.mic_owner() if meeting else None
        except OSError as exc:
            logger.debug("Presence failed: %s", exc)
            return
        changed = owner != self.mic_owner
        self.mic_owner = owner
        self.captions.set_mic_focus(owner in (None, self.target))
        if changed:
            self.broadcast()

    # -- state -----------------------------------------------------------------
    def state(self):
        session = self.session
        return {"type": "capture_state", "available": self.available, "reason": self.reason,
                "mode": self.mode, "active": session is not None,
                "since": session["start"] if session else None,
                "trigger": session["trigger"] if session else None,
                "file": session["file"].name if session else None,
                "snoozed": self.snoozed, "sound": self.detector.active, "level": round(self.level, 1),
                "quiet": self.quiet(), "silence": self.detector.silence,
                "target": self.target, "mic_owner": self.mic_owner, "dir": str(self.root),
                "notice": self.notice}

    def quiet(self):
        """Seconds the Target has been silent (None while it sounds or never sounded)."""
        quiet = self.detector.silent_for(time.monotonic())
        return None if quiet == float("inf") or quiet < 2 else int(quiet)

    def broadcast(self):
        self.publish(self.state())
