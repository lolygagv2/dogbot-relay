"""Replayer per-dog targets, previous-day landing, scoped reset, replay clock on status (2026-11-18)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth import Actor
from app.models import ReplayJump, ReplayLoad, ReplayTarget
from app.services import event_store, notify_service, push_service, replayer as rp, settings_service

ADMIN = Actor(kind="admin", id="user_t1", user_id="user_t1")
EVENTS = [
    {"offset_s": 0, "type": "status", "data": {"battery": 90, "mode": "idle", "treats_remaining": 8}},
    {"offset_s": 600, "type": "mode_change", "data": {"mode": "silent_guardian"}},
    {"offset_s": 1200, "type": "bark_episode", "duration_s": 60, "data": {"bark_count": 5, "severity": "med"}},
    {"offset_s": 1300, "type": "treat", "data": {"source": "auto"}},
    {"offset_s": 7200, "type": "mode_change", "data": {"mode": "idle"}},
]
GOOD = {"version": 1, "name": "good", "timezone": "UTC", "day_start": "07:00", "land_previous_day": True,
        "settings": {"timezone": "Asia/Tokyo", "quiet_hours": {"enabled": True, "start": "01:00", "end": "02:00"}},
        "events": EVENTS}
BAD = {"version": 1, "name": "bad", "timezone": "UTC", "day_start": "07:00", "events": EVENTS[:3]}


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def r(temp_db, tmp_path, monkeypatch):
    import app.database as db
    (tmp_path / "good.json").write_text(json.dumps(GOOD))
    (tmp_path / "bad.json").write_text(json.dumps(BAD))
    monkeypatch.setattr(rp, "seeds_dir", lambda: tmp_path)

    async def fake_push(**kw):
        return "sent"
    monkeypatch.setattr(push_service, "send_push", fake_push)
    monkeypatch.setattr(notify_service, "notify_agent", lambda *a, **k: None)
    db.create_dog(dog_id="dog_t3", name="Bex", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")
    db.create_device_pairing("user_t1", "robot_t")
    return rp.Replayer()


def replay_events(dog, date=None):
    from app.database import list_events
    return [e for e in list_events(dog, local_date=date, limit=1000) if e["source"] in ("replay", "relay")]


def today(dog="dog_t1"):
    return datetime.now(timezone.utc).astimezone(settings_service.get_timezone(dog)).date()


# ---- request models ----

def test_every_call_names_its_dog():
    for model, extra in ((ReplayTarget, {}), (ReplayLoad, {"seed": "good.json"}), (ReplayJump, {"to": "10:00"})):
        with pytest.raises(ValidationError):
            model.model_validate(extra)


# ---- load ----

def test_load_only_onto_an_owned_existing_dog(r):
    with pytest.raises(HTTPException) as e:
        run(r.load("good.json", ADMIN, dog_id="no_such_dog"))
    assert e.value.status_code == 404
    viewer = Actor(kind="admin", id="user_t2", user_id="user_t2")
    with pytest.raises(HTTPException) as e:
        run(r.load("good.json", viewer, dog_id="dog_t1"))
    assert e.value.status_code == 403 and e.value.detail["code"] == "not_dog_owner"
    from app.database import get_dog_by_id
    assert get_dog_by_id("demo_dog") is None


def test_load_never_touches_the_dogs_settings(r):
    tz_before = settings_service.get_value("dog_t1", "timezone")
    out = run(r.load("good.json", ADMIN, dog_id="dog_t1"))
    assert settings_service.get_value("dog_t1", "timezone") == tz_before
    assert settings_service.get_setting("dog_t1", "quiet_hours")["version"] == 0
    assert out["settings_ignored"] == ["quiet_hours", "timezone"]
    assert out["timezone"] == tz_before and out["device_id"] == "robot_t"   # the dog's own robot


def test_device_of_another_owner_is_refused(r):
    import app.database as db
    db.create_device_pairing("user_t2", "robot_other")
    with pytest.raises(HTTPException) as e:
        run(r.load("good.json", ADMIN, dog_id="dog_t1", device_id="robot_other"))
    assert e.value.status_code == 422 and e.value.detail["code"] == "device_not_dogs"


def test_good_day_lands_yesterday_and_replays_today(r):
    out = run(r.load("good.json", ADMIN, dog_id="dog_t1"))
    t = today()
    y = (t - timedelta(days=1)).isoformat()
    assert out["date"] == t.isoformat() and out["landed_date"] == y and out["landed"] == len(EVENTS)
    assert out["status"] == "loaded" and out["emitted"] == 0
    assert len(replay_events("dog_t1", y)) == len(EVENTS) and replay_events("dog_t1", t.isoformat()) == []
    assert event_store.get_summary("dog_t1", y, refresh=True)["counts"]["bark_episode"] == 1
    # loading again rewrites, never duplicates
    again = run(r.load("good.json", ADMIN, dog_id="dog_t1"))
    assert again["landed"] == len(EVENTS) and len(replay_events("dog_t1", y)) == len(EVENTS)


def test_bad_day_keeps_yesterdays_recap_and_clears_today(r):
    run(r.load("good.json", ADMIN, dog_id="dog_t1"))
    run(r.jump(to="23:00", dog_id="dog_t1"))
    t, y = today().isoformat(), (today() - timedelta(days=1)).isoformat()
    assert len(replay_events("dog_t1", t)) == len(EVENTS)
    out = run(r.load("bad.json", ADMIN, dog_id="dog_t1"))
    assert out["landed"] == 0 and out["landed_date"] is None
    assert replay_events("dog_t1", t) == [] and len(replay_events("dog_t1", y)) == len(EVENTS)


# ---- one replay, several dogs ----

def test_calls_for_another_dog_get_409_with_the_loaded_dog(r):
    run(r.load("bad.json", ADMIN, dog_id="dog_t1"))
    for call in (lambda: r.pause(dog_id="dog_t3"), lambda: r.start(dog_id="dog_t3"),
                 lambda: r.jump(to="10:00", dog_id="dog_t3"), lambda: r.set_speed(10, dog_id="dog_t3")):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert e.value.status_code == 409 and e.value.detail["code"] == "replay_other_dog"
        assert e.value.detail["loaded_dog_id"] == "dog_t1"
    st = r.state_for("dog_t3")
    assert st["status"] == "idle" and st["dog_id"] == "dog_t3" and st["virtual_time"] is None
    assert st["busy_with"]["dog_id"] == "dog_t1"
    assert r.state_for("dog_t1")["dog_id"] == "dog_t1" and r.state_for("dog_t1")["busy_with"] is None


def test_nothing_loaded_is_409_not_loaded(r):
    with pytest.raises(HTTPException) as e:
        run(r.pause(dog_id="dog_t1"))
    assert e.value.detail["code"] == "replay_not_loaded"


def test_loading_another_dog_takes_over_and_leaves_the_first_dogs_data(r):
    run(r.load("bad.json", ADMIN, dog_id="dog_t1"))
    run(r.jump(to="23:00", dog_id="dog_t1"))
    out = run(r.load("bad.json", ADMIN, dog_id="dog_t3"))
    assert out["dog_id"] == "dog_t3" and not r.handles("dog_t1")
    assert len(replay_events("dog_t1")) == 3


# ---- reset ----

def test_reset_deletes_only_replay_made_rows(r):
    from app.database import (get_event, insert_notify_log, insert_treat_ledger, link_treat_ledger,
                              list_events)
    real = {"event_id": "real1", "type": "treat", "dog_id": "dog_t1", "ts": "2026-11-10T12:00:00Z",
            "data": {"source": "app"}}
    run(event_store.ingest_events([real], device_id="robot_t", user_id="user_t1", source="robot",
                                  notify=False, forward=False))
    insert_treat_ledger("cmd_real", "dog_t1", "app:user_t1", "2026-11-10")
    link_treat_ledger("cmd_real", "real1")
    insert_notify_log("dog_t1", "user_t1", "bark_long", "real1", "treat", "sent", "t", "b", None, None)
    run(r.load("good.json", ADMIN, dog_id="dog_t1"))
    out = run(r.reset_dog("dog_t1", ADMIN))
    assert out["removed"] == len(EVENTS) and out["status"] == "idle" and not r.handles("dog_t1")
    assert replay_events("dog_t1") == [] and get_event("real1") is not None
    import app.database as db
    with db.db_connection() as conn:
        assert conn.execute("SELECT 1 FROM treat_ledger WHERE command_id='cmd_real'").fetchone()
        assert conn.execute("SELECT 1 FROM notify_log WHERE event_id='real1'").fetchone()


def test_reset_another_dog_works_while_a_replay_runs(r):
    run(r.load("good.json", ADMIN, dog_id="dog_t3"))            # dog_t3 has a recap on yesterday
    run(r.load("bad.json", ADMIN, dog_id="dog_t1"))             # now dog_t1 is loaded
    out = run(r.reset_dog("dog_t3", ADMIN))
    assert out["removed"] == len(EVENTS) and out["dog_id"] == "dog_t3" and out["busy_with"]["dog_id"] == "dog_t1"
    assert r.handles("dog_t1") and replay_events("dog_t3") == []


# ---- jump counts, status clock ----

def test_jump_reports_landed_and_removed(r):
    run(r.load("bad.json", ADMIN, dog_id="dog_t1"))
    fwd = run(r.jump(to="07:25", dog_id="dog_t1"))           # offsets 0, 600, 1200 -> 3 events
    assert fwd["landed"] == 3 and fwd["removed"] == 0
    back = run(r.jump(to="07:05", dog_id="dog_t1"))          # keeps offset 0 only
    assert back["landed"] == 0 and back["removed"] == 2 and back["emitted"] == 1


def test_status_carries_the_replay_clock_for_that_dog_only(r, monkeypatch):
    from app.routers import event_store as routes
    monkeypatch.setattr(rp, "get_replayer", lambda: r)
    owner = Actor(kind="app", id="user_t1", user_id="user_t1")
    st = run(routes.get_dog_status("dog_t1", actor=owner))
    assert st["replay_active"] is False and st["virtual_time"] is None and st["replay_date"] is None
    run(r.load("bad.json", ADMIN, dog_id="dog_t1"))
    run(r.jump(to="10:00", dog_id="dog_t1"))
    st = run(routes.get_dog_status("dog_t1", actor=owner))
    assert st["replay_active"] is True and st["replay_date"] == today().isoformat()
    tz = settings_service.get_timezone("dog_t1")
    assert datetime.fromisoformat(st["virtual_time"]).astimezone(tz).strftime("%H:%M") == "10:00"
    other = run(routes.get_dog_status("dog_t3", actor=owner))
    assert other["replay_active"] is False and other["virtual_time"] is None
