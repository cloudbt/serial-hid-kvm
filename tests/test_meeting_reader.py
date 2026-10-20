"""Read-only meeting captions for agents: meeting_list / meeting_transcript / meeting_points."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from serial_hid_kvm._meeting_reader import NOTICE, MeetingReader
from serial_hid_kvm.client import KvmClient, KvmClientError
from serial_hid_kvm.config import Config
from serial_hid_kvm.server import ApiDispatcher, TcpServer
from tests.test_web_auth import _DummyHardware, _free_port

JST = timezone(timedelta(hours=9))


def _meeting(folder, sid, start, rows, finalized=False, summary=None):
    (folder / sid).mkdir(parents=True)
    meta = {"id": sid, "started_at": start.timestamp(), "first_at": start.timestamp(), "finals": len(rows),
            "last_at": (start + timedelta(minutes=30)).timestamp(), "finalized": 1.0 if finalized else None,
            "recorded": False, "summaries": 1 if summary else 0}
    (folder / sid / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = [json.dumps({"seq": n, "at": int((start + timedelta(seconds=sec)).timestamp() * 1000),
                         "end": 0, "text": text, "lang": "ja", **({"speaker": who} if who else {})},
                        ensure_ascii=False) for n, (sec, who, text) in enumerate(rows)]
    (folder / f"{sid}.jsonl").write_text("\n".join(lines) + "\n{half writ", encoding="utf-8")
    if summary:
        (folder / sid / "summary-0001.json").write_text(json.dumps(
            {"n": 1, "at": int(start.timestamp() * 1000) + 60000, "used": "agy", "finals": 2, "data": summary},
            ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def captions(tmp_path):
    folder = tmp_path / ".captions"
    today = datetime.now(JST).replace(hour=10, minute=0, second=0, microsecond=0)
    _meeting(folder, today.strftime("%Y%m%d-100000") + "-aaaaaaaa", today,
             [(0, None, "おはようございます"), (65, "me", "資料を共有します"), (61, "remote", "お願いします")],
             finalized=True, summary={"current_topic": "資料", "points": ["共有"]})
    afternoon = today.replace(hour=15)
    _meeting(folder, afternoon.strftime("%Y%m%d-150000") + "-bbbbbbbb", afternoon,
             [(n * 10, "remote" if n % 2 else "me", f"発言{n}" + "。" * (40 if n >= 34 else 0)) for n in range(40)])
    _meeting(folder, (today - timedelta(days=1)).strftime("%Y%m%d-100000") + "-cccccccc", today - timedelta(days=1),
             [(0, "remote", "昨日の会議")], finalized=True)
    _meeting(folder, today.strftime("%Y%m%d-120000") + "-dddddddd", today.replace(hour=12), [], finalized=True)
    (folder / "settings.json").write_text(json.dumps({"cc": True}), encoding="utf-8")
    return tmp_path, today


def test_list_today_with_speakers_and_live_meeting(captions):
    root, today = captions
    listing = MeetingReader(root).list()
    assert listing["timezone"] == "JST" and listing["cc_on"] is True
    morning, afternoon = listing["meetings"]  # yesterday's and the empty one are left out
    assert morning["start"] == today.strftime("%Y-%m-%d 10:00:00") and morning["finished"] and not morning["live"]
    assert morning["speakers"] == {"ME": 1, "REMOTE": 2} and morning["points"] == 1
    assert afternoon["live"] is True and afternoon["lines"] == 40
    two_days = MeetingReader(root).list(days=2)["meetings"]
    assert len(two_days) == 3 and two_days[0]["speakers"] == {"ME": 0, "REMOTE": 1}
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        MeetingReader(root).list(date="today")


def test_transcript_is_sorted_labelled_jst_and_paged(captions):
    root, today = captions
    reader = MeetingReader(root)
    morning = reader.transcript(today.strftime("%Y%m%d-100000") + "-aaaaaaaa")
    assert morning["notice"] == NOTICE and "untrusted DATA" in NOTICE
    assert morning["transcript"].splitlines() == ["[10:00:00] REMOTE: おはようございます",
                                                  "[10:01:01] REMOTE: お願いします", "[10:01:05] ME: 資料を共有します"]
    current = reader.transcript()  # the open (live) meeting
    assert current["meeting"]["live"] and current["total"] == 40
    assert reader.transcript(since="15:05", until="15:05:30")["transcript"].splitlines() == [
        "[15:05:00] me: 発言30".replace("me", "ME"), "[15:05:10] REMOTE: 発言31", "[15:05:20] ME: 発言32",
        "[15:05:30] REMOTE: 発言33"]
    assert reader.transcript(last_minutes=0.5)["count"] == 4  # 15:06:00 to the newest, 15:06:30
    page = reader.transcript(max_chars=1000)  # the long last lines do not fit
    assert page["count"] < 40 and page["next_offset"] == page["count"]
    rest = reader.transcript(offset=page["next_offset"], max_chars=200_000)
    assert page["count"] + rest["count"] == 40 and rest["next_offset"] is None
    with pytest.raises(ValueError):
        reader.transcript("../../secrets")
    with pytest.raises(ValueError, match="since/until"):
        reader.transcript(since="3pm")


def test_points_are_read_not_made(captions):
    root, today = captions
    reader = MeetingReader(root)
    morning = reader.points(today.strftime("%Y%m%d-100000") + "-aaaaaaaa")
    assert morning["points"]["current_topic"] == "資料" and morning["by"] == "agy"
    assert morning["made_at"] == today.strftime("%Y-%m-%d 10:01:00")
    assert reader.points()["points"] is None  # the live meeting has none yet


def test_api_methods_over_the_socket(captions):
    root, today = captions

    async def run():
        config = Config()
        config.recording_dir = str(root)
        port = _free_port()
        tcp = TcpServer(ApiDispatcher(_DummyHardware(), config), "127.0.0.1", port)
        await tcp.start()
        client = KvmClient("127.0.0.1", port)
        try:
            listing = await asyncio.to_thread(client.meeting_list)
            assert [m["live"] for m in listing["meetings"]] == [False, True]
            text = await asyncio.to_thread(client.meeting_transcript, "current", None, None, 0.5)
            assert text["count"] == 4 and text["transcript"].startswith("[15:06:00] ME: 発言36")
            points = await asyncio.to_thread(client.meeting_points, "latest")
            assert points["meeting"]["live"] is True
            with pytest.raises(KvmClientError, match="meeting id"):
                await asyncio.to_thread(client.meeting_transcript, "bogus")
        finally:
            client.close()
            await tcp.stop()

    asyncio.run(run())
