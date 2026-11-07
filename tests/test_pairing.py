"""Pairing 2026-11-07: device_dogs + placeholder dogs; a robot is never blocked on pairing."""
import asyncio

import app.database as db
from app.auth import Actor
from app.models import DeviceDogsUpdate, EventBatchIn
from app.routers import event_store as routes
from app.routers import user as user_routes
from app.services import event_store, pairing, robot_control

ROBOT = Actor(kind="robot", id="wimz_robot_05", user_id="user_t1", device_id="wimz_robot_05")
OWNER = Actor(kind="app", id="user_t1", user_id="user_t1")
PH = "ph_user_t1_wimz_robot_05"


def _post(events, actor=ROBOT):
    return asyncio.run(routes.post_events(EventBatchIn.model_validate({"events": events}), actor=actor))


def _owner_without_dogs():
    db.delete_dog("dog_t1")  # user_t1 now has no dogs
    db.create_device_pairing("user_t1", "wimz_robot_05")


def test_placeholder_name():
    assert pairing.placeholder_name("wimz_robot_05") == "Robot 05's dog"
    assert pairing.placeholder_name("garage-bot") == "garage-bot's dog"


def test_unowned_robot_gets_no_dog(temp_db):
    assert pairing.device_dog_ids("wimz_robot_99") == []


def test_placeholder_created_once_and_paired(temp_db):
    _owner_without_dogs()
    assert pairing.device_dog_ids("wimz_robot_05") == [PH]
    dog = db.get_dog_by_id(PH)
    assert dog["name"] == "Robot 05's dog" and dog["is_placeholder"] is True
    assert db.get_dog_owner(PH) == "user_t1" and db.get_user_dog_role("user_t1", PH) == "owner"
    assert pairing.device_dog_ids("wimz_robot_05") == [PH]  # idempotent
    assert db.get_dog_count() == 1


def test_placeholder_works_end_to_end(temp_db):
    _owner_without_dogs()
    res = _post([
        {"event_id": "b1", "type": "bark_episode", "ts": "2026-11-06T15:00:00Z", "duration_s": 60,
         "data": {"bark_count": 9, "severity": "med"}},                      # no dog_id: placeholder
        {"event_id": "s1", "type": "settle", "ts": "2026-11-06T15:02:00Z", "data": {"episode_id": "b1"}},
        {"event_id": "st1", "type": "status", "ts": "2026-11-06T15:03:00Z", "data": {"battery": 64, "mode": "idle"}},
    ])
    assert res["accepted"] == ["b1", "s1", "st1"] and res["rejected"] == []
    assert db.get_event("b1")["dog_id"] == PH and db.get_event("st1")["dog_id"] is None
    summary = event_store.get_summary(PH, "2026-11-06")
    assert summary["counts"]["bark_episode"] == 1 and summary["time_to_settle"]["n"] == 1
    st = asyncio.run(routes.get_dog_status(PH, actor=OWNER))
    assert st["battery"] == 64 and st["device_id"] == "wimz_robot_05"
    # commands for the placeholder target its robot; utterances/hooks resolve it
    assert robot_control._pick_device("user_t1", None, PH)[0] == "wimz_robot_05"
    assert pairing.primary_dog_id("wimz_robot_05") == PH
    from app.voice_agent.events import _pick_dog
    assert _pick_dog("user_t1", None, "wimz_robot_05")["id"] == PH


def test_rename_keeps_events_and_clears_flag(temp_db):
    _owner_without_dogs()
    _post([{"event_id": "t1", "type": "treat", "ts": "2026-11-06T15:00:00Z", "data": {"source": "auto"}}])
    db.update_dog(PH, name="Bezik")
    assert db.get_dog_by_id(PH)["is_placeholder"] is False
    assert db.get_event("t1")["dog_id"] == PH and pairing.device_dog_ids("wimz_robot_05") == [PH]


def test_repair_keeps_history_and_merge_moves_it(temp_db):
    _owner_without_dogs()
    _post([{"event_id": "t1", "type": "treat", "ts": "2026-11-06T15:00:00Z", "data": {"source": "auto"}}])
    db.create_dog(dog_id="dog_real", name="Rex", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_real", "owner")
    out = asyncio.run(user_routes.put_device_dogs("wimz_robot_05", DeviceDogsUpdate(dog_ids=["dog_real"]),
                                                  current_user={"user_id": "user_t1"}))
    assert [d["dog_id"] for d in out["dogs"]] == ["dog_real"]
    assert db.get_event("t1")["dog_id"] == PH                      # unpaired, not lost
    assert event_store.get_summary(PH, "2026-11-06")["totals"]["treats"] == 1
    db.merge_dogs("user_t1", "dog_real", [PH])
    assert db.get_event("t1")["dog_id"] == "dog_real" and db.get_dog_by_id(PH) is None
    assert event_store.get_summary("dog_real", "2026-11-06")["totals"]["treats"] == 1


def test_repair_rejects_dogs_of_other_users(temp_db):
    import pytest
    from fastapi import HTTPException
    _owner_without_dogs()
    db.create_dog(dog_id="dog_x", name="X", user_id="user_t2")
    db.add_user_dog("user_t2", "dog_x", "owner")
    with pytest.raises(HTTPException) as e:
        asyncio.run(user_routes.put_device_dogs("wimz_robot_05", DeviceDogsUpdate(dog_ids=["dog_x"]),
                                                current_user={"user_id": "user_t1"}))
    assert e.value.status_code == 400


def test_new_owner_does_not_inherit_old_owners_dogs(temp_db):
    db.create_device_pairing("user_t1", "wimz_robot_05")
    db.set_device_dogs("wimz_robot_05", ["dog_t1"])
    db.create_device_pairing("user_t2", "wimz_robot_05")        # handed to another user
    assert db.get_device_dog_ids("wimz_robot_05") == []
    assert pairing.device_dog_ids("wimz_robot_05") == ["ph_user_t2_wimz_robot_05"]


def test_backfill_links_owner_dogs_when_table_is_created(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "pre.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.create_user("u1", "a@example.com", "x")
    db.create_dog(dog_id="d1", name="A", user_id="u1")
    db.add_user_dog("u1", "d1", "owner")
    db.create_device_pairing("u1", "r1")
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE device_dogs")                       # simulate the pre-pairing schema
    conn.commit()
    conn.close()
    db.init_db()
    assert db.get_device_dog_ids("r1") == ["d1"]
