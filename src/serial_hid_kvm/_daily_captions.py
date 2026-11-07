"""Daily caption files: ``meetings/<YYYY-MM-DD>.md``, one per Target and JST day.

Rendered from what is already on disk, so a file can always be rebuilt:

- CC meetings: ``recording_dir/.captions/<id>.jsonl`` + ``<id>/meta.json``
- capture sessions: ``meetings/audio/<day>/<stamp>.json`` (+ ``.mka``)
- backfilled lines: ``meetings/.backfill/<stamp>.jsonl`` (marked 补)
- video recordings: ``recording_dir/recording-<JST stamp>-*.mp4``

A meeting belongs to the day it started.  Every section is headed with its
JST start-end time; lines read ``- [HH:MM:SS] 对方/我：text``.  The raw files
are never changed or deleted here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re

from ._meeting_reader import MeetingReader
from ._sop_store import atomic_text

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
SPEAKER = {"me": "我", "remote": "对方"}
JOIN = 5 * 60          # an audio file within 5 min of a meeting belongs to it
VIDEO_STAMP = re.compile(r"^recording-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})")


def _clock(epoch, seconds=False):
    return datetime.fromtimestamp(epoch, JST).strftime("%H:%M:%S" if seconds else "%H:%M")


def _day(epoch):
    return datetime.fromtimestamp(epoch, JST).date()


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _jsonl(path):
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
            at = float(row["at"]) / 1000
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(row.get("text"), str) and row["text"].strip():
            rows.append((at, "me" if row.get("speaker") == "me" else "remote", " ".join(row["text"].split()),
                         row.get("source") == "batch"))
    return rows


class DailyCaptions:
    def __init__(self, recording_dir, meetings_dir, target=""):
        self.recordings = Path(recording_dir).expanduser()
        self.root = Path(meetings_dir).expanduser()
        self.target = target
        self.reader = MeetingReader(self.recordings)

    def path(self, day):
        return self.root / f"{day.isoformat()}.md"

    # -- inputs ---------------------------------------------------------------
    def _audio(self, around=None):
        """Capture sessions; with *around* (a date) only the folders of that day ± 1."""
        items = []
        if around is None:
            sides = (self.root / "audio").glob("*/*.json")
        else:
            sides = [side for d in (around - timedelta(days=1), around, around + timedelta(days=1))
                     for side in (self.root / "audio" / d.isoformat()).glob("*.json")]
        for side in sides:
            data = _read_json(side)
            if not data or not data.get("start"):
                continue
            media = side.with_suffix(".mka")
            items.append({"start": float(data["start"]),
                          "end": float(data["end"]) if data.get("end") else None,
                          "file": media.relative_to(self.root).as_posix(),
                          "backfill": self.root / ".backfill" / (side.stem + ".jsonl")})
        return sorted(items, key=lambda a: a["start"])

    def _videos(self):
        items = []
        for mp4 in self.recordings.glob("recording-*.mp4"):
            match = VIDEO_STAMP.match(mp4.name)
            if not match:
                continue
            try:
                start = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H %M %S").replace(tzinfo=JST).timestamp()
                end = mp4.stat().st_mtime
            except (ValueError, OSError):
                continue
            items.append({"start": start, "end": max(end, start), "name": mp4.name})
        return items

    def days(self):
        """Every JST day with a meeting or a capture session."""
        found = set()
        for meta in self.reader._metas():
            if meta.get("finals"):
                found.add(_day(float(meta.get("first_at") or meta.get("started_at") or 0)))
        for audio in self._audio():
            found.add(_day(audio["start"]))
        return sorted(found)

    # -- meetings of one day ----------------------------------------------------
    def meetings(self, day):
        """[{start, end, live, rows[], audio[], video[]}] that started on *day*."""
        audio = self._audio(day)
        used = set()
        meetings = []
        # A CC session stays open for a while after CC goes off (resumable), so
        # it is only 进行中 while CC is switched on; an automatic capture session
        # (CC not saved) counts through its audio file below.
        cc_on = self.reader._cc_on()
        for meta in self.reader._metas():
            if not meta.get("finals"):
                continue
            begun = _day(float(meta.get("first_at") or meta.get("started_at") or 0))
            if not day - timedelta(days=1) <= begun <= day:   # only read the files that can matter
                continue
            rows = [(at, sp, text, False) for at, sp, text in self.reader._rows(meta["id"])]
            if not rows:
                continue
            start, end = rows[0][0], rows[-1][0]
            meetings.append({"start": start, "end": end, "live": cc_on and not meta.get("finalized"), "rows": rows,
                             "audio": [], "video": []})
        for m in meetings:
            for i, a in enumerate(audio):
                a_end = a["end"] or a["start"]
                if a["start"] <= m["end"] + JOIN and a_end >= m["start"] - JOIN:
                    m["audio"].append(a)
                    used.add(i)
        for i, a in enumerate(audio):        # recorded but never captioned (hub offline)
            if i not in used:
                meetings.append({"start": a["start"], "end": a["end"] or a["start"], "live": a["end"] is None,
                                 "rows": [], "audio": [a], "video": []})
        videos = [v for v in self._videos() if abs(_day(v["start"]) - day) <= timedelta(days=1)]
        result = []
        for m in sorted(meetings, key=lambda m: m["start"]):
            for a in m["audio"]:
                m["rows"] += _jsonl(a["backfill"])
                m["live"] = m["live"] or a["end"] is None
            m["rows"].sort(key=lambda r: r[0])
            if m["rows"]:   # the time span is the speech; audio alone adds pre-roll and trailing silence
                m["start"], m["end"] = m["rows"][0][0], m["rows"][-1][0]
            m["video"] = [v for v in videos if v["start"] <= m["end"] + 60 and v["end"] >= m["start"] - 60]
            if _day(m["start"]) == day:
                result.append(m)
        return result

    # -- rendering ----------------------------------------------------------------
    def view(self, day):
        """JSON for the viewer's 今天 tab: meetings with rows [epoch ms, speaker, text, backfilled]."""
        return {"type": "captions_day", "date": day.isoformat(), "file": str(self.path(day)),
                "meetings": [{"start": m["start"], "end": m["end"], "live": m["live"],
                              "rows": [[int(at * 1000), speaker, text, bool(batch)]
                                       for at, speaker, text, batch in m["rows"]]}
                             for m in self.meetings(day)]}

    def render(self, day):
        meetings = self.meetings(day)
        title = f"# {day.isoformat()} 会议字幕" + (f" · {self.target}" if self.target else "")
        lines = [title, "",
                 "> 自动生成，时间为日本时间（JST）。「对方」= Target 的 HDMI 声音，「我」= 本机麦克风；"
                 "标「补」的行是字幕服务离线时事后从录音转写的。原始字幕和录音另有保存，不会被删除。", ""]
        if not meetings:
            lines += ["（这一天没有会议字幕）", ""]
        for m in meetings:
            minutes = max(0, round((m["end"] - m["start"]) / 60))
            span = (f"{_clock(m['start'])}–（进行中）" if m["live"]
                    else f"{_clock(m['start'])}–{_clock(m['end'])}（{minutes} 分钟）")
            lines += [f"## {span}", ""]
            for a in m["audio"]:
                lines.append(f"- 录音：[{Path(a['file']).name}]({a['file']})")
            for v in m["video"]:
                lines.append(f"- 录像：[{v['name']}](../{self.recordings.name}/{v['name']})")
            if m["audio"] or m["video"]:
                lines.append("")
            if not m["rows"]:
                lines += ["（没有字幕：字幕服务离线，录音会在服务恢复后补转写）", ""]
                continue
            for at, speaker, text, batch in m["rows"]:
                mark = "补·" if batch else ""
                lines.append(f"- [{_clock(at, True)}] {mark}{SPEAKER[speaker]}：{text}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def write(self, day):
        path = self.path(day)
        text = self.render(day)
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_text(path, text)
        return path

    def write_missing(self, today=None):
        """At startup: every day with meetings but no file yet, plus today."""
        today = today or datetime.now(JST).date()
        written = []
        for day in self.days():
            if day == today or not self.path(day).exists():
                written.append(self.write(day))
        return written
