"""Daily roll-up: validation, time-to-settle, top episodes, alone time."""
import asyncio

import pytest

from app.services import event_store


def _ev(eid, etype, ts, duration=None, **data):
    return {"event_id": eid, "type": etype, "dog_id": "dog_t1", "ts": ts, "duration_s": duration, "data": data}


def test_validate_rejects_missing_required():
    ev, reason = event_store.validate_event(_ev("a", "bark_episode", "2026-11-06T15:00:00Z", bark_count=3, severity="low"))
    assert ev is None and "duration_s" in reason
    ev, reason = event_store.validate_event(_ev("b", "treat", "2026-11-06T15:00:00Z"))
    assert ev is None and "source" in reason
    ev, reason = event_store.validate_event(_ev("c", "status", "2026-11-06T15:00:00Z", battery=50, mode="idle", light_level=999, docked=1))
    assert ev is not None and ev["data"]["light_level"] == 255 and ev["data"]["docked"] is True


def test_media_urls_are_whitelisted():
    ev, _ = event_store.validate_event(_ev("d", "bark_episode", "2026-11-06T15:00:00Z", 30, bark_count=2, severity="med",
                                           image_url="https://evil.example/x.jpg", clip_url="/api/media/download/ok.mp4"))
    assert "image_url" not in ev["data"] and ev["data"]["clip_url"] == "/api/media/download/ok.mp4"


def test_rollup_time_to_settle_and_top_episodes(temp_db):
    base = "2026-11-06T15:00:00Z"
    events = [
        _ev("dep", "departure", "2026-11-06T13:00:00Z", method="door"),
        _ev("ep1", "bark_episode", base, 90, bark_count=20, severity="med"),
        _ev("i1", "intervention", "2026-11-06T15:00:30Z", kind="sound", episode_id="ep1"),
        _ev("s1", "settle", "2026-11-06T15:01:15Z", episode_id="ep1"),
        _ev("ep2", "bark_episode", "2026-11-06T16:00:00Z", 200, bark_count=50, severity="high"),
        _ev("s2", "settle", "2026-11-06T16:03:20Z"),              # no episode_id: window match
        _ev("ep3", "bark_episode", "2026-11-06T17:00:00Z", 40, bark_count=5, severity="low"),  # unresolved
        _ev("t1", "treat", "2026-11-06T15:02:00Z", source="auto", count=2),
        _ev("arr", "arrival", "2026-11-06T18:00:00Z", method="door"),
        _ev("st", "status", "2026-11-06T18:00:00Z", battery=80, mode="idle"),
    ]
    res = asyncio.run(event_store.ingest_events(events, device_id="robot_t", user_id="user_t1", source="robot",
                                                notify=False, forward=False))
    assert res["rejected"] == [] and len(res["accepted"]) == len(events)
    # idempotent
    res2 = asyncio.run(event_store.ingest_events(events[:1], device_id="robot_t", user_id="user_t1", source="robot",
                                                 notify=False, forward=False))
    assert res2["duplicate"] == ["dep"]

    s = event_store.get_summary("dog_t1", "2026-11-06")
    assert s["counts"]["bark_episode"] == 3 and s["counts"]["settle"] == 2 and s["counts"]["treat"] == 1
    assert s["totals"]["treats"] == 2 and s["totals"]["bark_s"] == 330
    assert s["totals"]["alone_s"] == 5 * 3600
    tts = s["time_to_settle"]
    assert tts["n"] == 2 and tts["unresolved"] == 1 and tts["min_s"] == 75 and tts["max_s"] == 200
    top = s["top_episodes"]
    assert [e["event_id"] for e in top] == ["ep2", "ep1", "ep3"]
    assert top[1]["interventions"] == ["sound"] and top[1]["settle_s"] == 75 and top[2]["settle_s"] is None
    # settle_s was derived at ingest for s1
    from app.database import get_event
    assert get_event("s1")["data"]["settle_s"] == 75
    # cache: second read served from daily_summary, refresh recomputes
    assert event_store.get_summary("dog_t1", "2026-11-06")["event_count"] == 9


def test_treat_still_is_pinned_for_seven_days(temp_db, tmp_path, monkeypatch):
    from app.services import media_files
    monkeypatch.setattr(media_files, "media_dir", lambda: tmp_path)
    (tmp_path / "robot_t_ab12.jpg").write_bytes(b"x")
    ev = _ev("tr1", "treat", "2026-11-06T15:00:00Z", source="auto", image_url="/api/media/download/robot_t_ab12.jpg")
    asyncio.run(event_store.ingest_events([ev], device_id="robot_t", user_id="user_t1", source="robot", notify=False, forward=False))
    assert (tmp_path / "robot_t_ab12.jpg.ttl").read_text() == str(7 * 24 * 3600)
    assert media_files.resolve("robot_t_ab12.jpg") is not None
    assert media_files.resolve("../etc/passwd") is None
