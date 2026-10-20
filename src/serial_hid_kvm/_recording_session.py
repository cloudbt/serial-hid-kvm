"""Resumable, ordered recording uploads independent of viewer connections."""
import json
import os
from pathlib import Path
import re
import threading
from datetime import datetime
import math
import time


class RecordingSessions:
    """Persist acknowledged offsets; reconnect/retry must never duplicate bytes.

    One session is one uninterrupted browser MediaRecorder, from manual start
    to manual finish. Sources and manifests stay out of the final MP4 folder.
    """
    MAX_CHUNK = 256 * 1024

    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.lock = threading.RLock()

    def _paths(self, session):
        if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session):
            raise ValueError("Invalid recording session ID")
        folder = self.directory / ".recording-sessions" / session
        return folder / "session.json", folder / "source.webm"

    @staticmethod
    def _save(path, state):
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)

    def attach(self, session, filename, options=None):
        with self.lock:
            manifest, source = self._paths(session)
            if manifest.exists():
                state = json.loads(manifest.read_text(encoding="utf-8"))
                if source.stat().st_size < state["size"]:
                    raise ValueError("Saved recording is shorter than its acknowledged offset")
                # Discard a write that wasn't committed to the manifest before
                # a process crash. The client still has this unacknowledged block.
                if source.stat().st_size > state["size"]:
                    with source.open("r+b") as file:
                        file.truncate(state["size"])
                return state
            if not isinstance(filename, str) or not re.fullmatch(r"recording-[A-Za-z0-9._-]+\.webm", filename):
                raise ValueError("Invalid recording filename")
            manifest.parent.mkdir(parents=True, exist_ok=True)
            source.touch(exist_ok=False)
            # Include the session ID to keep distinct tabs with equal clocks
            # from publishing the same filename.
            state = {"id":session, "filename":Path(filename).stem + "-" + session[:8] + ".mp4",
                     "next":0, "size":0, "finished":False,
                     # Server clock at first attach: origin for live captions.
                     "started_epoch":time.time()}
            minutes = (options or {}).get("minutes") if isinstance(options, dict) else None
            if minutes in ("agy", "codex", "mac-qwen", "off"):
                state["minutes"] = minutes
            self._save(manifest, state)
            return state

    def write(self, session, sequence, data):
        with self.lock:
            manifest, source = self._paths(session)
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if sequence < state["next"]:
                return state  # ACK was lost; this block is already durable.
            if state["finished"]:
                raise ValueError("Recording already finished")
            if sequence != state["next"]:
                raise ValueError("Out-of-order recording block")
            if not data or len(data) > self.MAX_CHUNK:
                raise ValueError("Invalid recording block size")
            with source.open("r+b") as file:
                file.seek(state["size"])
                file.write(data)
                file.truncate()
                file.flush()
                os.fsync(file.fileno())
            state["size"] += len(data)
            state["next"] += 1
            self._save(manifest, state)
            return state

    def finish(self, session, expected, metadata=None):
        with self.lock:
            manifest, source = self._paths(session)
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if expected != state["next"]:
                raise ValueError("Recording still has unacknowledged data")
            if not state["size"]:
                raise ValueError("Recording is empty")
            state["finished"] = True
            if isinstance(metadata, dict) and not state.get('recorded_end_at'):
                # Optional browser facts must never prevent durable saving.
                try:
                    ended = datetime.fromisoformat(str(metadata.get('recorded_end_at', '')).replace('Z', '+00:00'))
                    duration = float(metadata.get('duration_seconds', 0))
                    if ended.tzinfo and math.isfinite(duration) and 0 <= duration <= 7*24*3600:
                        state['recorded_end_at'] = ended.isoformat()
                        state['duration_seconds'] = duration
                except (ValueError, TypeError):
                    pass
            self._save(manifest, state)
            return source, self.directory / state["filename"]

    # ---- server-side recordings (written by ffmpeg, not uploaded) ----------
    SERVER_SOURCE = "source.mkv"

    def create_server(self, session, filename, options=None):
        """New session whose source is recorded on the server (MKV)."""
        with self.lock:
            manifest, _ = self._paths(session)
            if manifest.exists():
                raise ValueError("Recording session already exists")
            if not isinstance(filename, str) or not re.fullmatch(r"recording-[A-Za-z0-9._-]+", filename):
                raise ValueError("Invalid recording filename")
            manifest.parent.mkdir(parents=True, exist_ok=False)
            state = {"id":session, "filename":filename + "-" + session[:8] + ".mp4",
                     "source":self.SERVER_SOURCE, "server":True, "next":0, "size":0,
                     "finished":False, "started_epoch":time.time()}
            options = options if isinstance(options, dict) else {}
            if options.get("minutes") in ("agy", "codex", "mac-qwen", "off"):
                state["minutes"] = options["minutes"]
            for key in ("fps", "audio_source"):
                if options.get(key) is not None:
                    state[key] = options[key]
            self._save(manifest, state)
            return state, manifest.with_name(self.SERVER_SOURCE)

    def finish_server(self, session, duration_seconds=None, recorded_end_at=None):
        with self.lock:
            manifest, _ = self._paths(session)
            state = json.loads(manifest.read_text(encoding="utf-8"))
            source = manifest.with_name(state.get("source", self.SERVER_SOURCE))
            size = source.stat().st_size if source.exists() else 0
            if not size:
                raise ValueError("Recording is empty")
            state.update(finished=True, size=size)
            if recorded_end_at is not None and duration_seconds is not None:
                state["recorded_end_at"] = recorded_end_at
                state["duration_seconds"] = duration_seconds
            self._save(manifest, state)
            return source, self.directory / state["filename"]

    def unfinished_server_sessions(self):
        """Server sessions left open by a crash or restart (oldest first)."""
        found = []
        for manifest in sorted((self.directory / ".recording-sessions").glob("*/session.json")):
            try:
                state = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.get("server") and not state.get("finished"):
                found.append(state["id"])
        return found
