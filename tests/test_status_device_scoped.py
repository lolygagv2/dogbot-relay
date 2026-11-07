"""Contract fix 2026-11-06: status events are device-scoped.

dog_id is optional for type "status" only; a dog-less status is resolved on ingest to the
robot's paired dog(s), or stored with dog_id NULL keyed by device_id when the robot has none.
The dog status route reads the latest status by the paired robot's device_id, not by dog_id.
"""
import asyncio

import pytest
from pydantic import ValidationError

import app.database as db
from app.auth import Actor
from app.models import EventBatchIn, EventIn
from app.routers import event_store as routes
from app.services import event_store


def _status(eid, ts="2026-11-06T18:00:00Z", dog_id=None, **data):
    ev = {"event_id": eid, "type": "status", "ts": ts, "data": {"battery": 77, "mode": "idle", **data}}
    if dog_id:
        ev["dog_id"] = dog_id
    return ev


def _ingest(events, device_id="robot_t", user_id="user_t1"):
    return asyncio.run(event_store.ingest_events(events, device_id=device_id, user_id=user_id,
                                                 source="robot", notify=False, forward=False))


def test_eventin_dog_id_optional_only_for_status():
    ev = EventIn.model_validate(_status("s0"))
    assert ev.dog_id is None
    with pytest.raises(ValidationError):
        EventIn.model_validate({"event_id": "t0", "type": "treat", "ts": "2026-11-06T18:00:00Z",
                                "data": {"source": "auto"}})
    ok = EventIn.model_validate({"event_id": "t1", "type": "treat", "dog_id": "dog_t1",
                                 "ts": "2026-11-06T18:00:00Z", "data": {"source": "auto"}})
    assert ok.dog_id == "dog_t1"
    # validate_event (used by replayer/admin paths too) applies the same rule
    assert event_store.validate_event({"event_id": "t2", "type": "treat", "ts": "2026-11-06T18:00:00Z",
                                       "data": {"source": "auto"}}) == (None, "dog_id required")


def test_unpaired_robot_status_stored_with_null_dog(temp_db):
    res = _ingest([_status("s_unpaired")], device_id="robot_lonely")
    assert res["accepted"] == ["s_unpaired"]
    row = db.get_event("s_unpaired")
    assert row["dog_id"] is None and row["device_id"] == "robot_lonely"
    assert db.get_latest_status_for_devices(["robot_lonely"])["event_id"] == "s_unpaired"
    assert _ingest([_status("s_unpaired")], device_id="robot_lonely")["duplicate"] == ["s_unpaired"]


def test_paired_robot_status_resolves_to_paired_dog(temp_db):
    db.create_device_pairing("user_t1", "robot_t")
    assert db.get_device_dog_ids("robot_t") == ["dog_t1"]
    res = _ingest([_status("s_one")])
    assert res["accepted"] == ["s_one"]
    assert db.get_event("s_one")["dog_id"] == "dog_t1"


def test_status_fans_out_to_every_paired_dog_idempotently(temp_db):
    db.create_device_pairing("user_t1", "robot_t")
    db.create_dog(dog_id="dog_t3", name="Bea", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")
    res = _ingest([_status("s_multi")])
    assert res["accepted"] == ["s_multi"]
    assert db.get_event("s_multi")["dog_id"] == "dog_t1"
    assert db.get_event("s_multi:dog_t3")["dog_id"] == "dog_t3"
    assert _ingest([_status("s_multi")])["duplicate"] == ["s_multi"]


def test_robot_post_without_dog_id_skips_dog_check(temp_db):
    db.create_device_pairing("user_t1", "robot_t")
    robot = Actor(kind="robot", id="robot_t", user_id="user_t1", device_id="robot_t")
    batch = EventBatchIn.model_validate({"events": [_status("s_http")]})
    res = asyncio.run(routes.post_events(batch, actor=robot))
    assert res["accepted"] == ["s_http"] and res["rejected"] == []


def test_dog_status_reads_latest_by_paired_device(temp_db):
    db.create_device_pairing("user_t1", "robot_t")
    _ingest([_status("s_old", ts="2026-11-06T17:00:00Z", battery=10)])
    _ingest([_status("s_new", ts="2026-11-06T18:00:00Z", battery=90, docked=True)])
    # a status explicitly tagged with this dog but from an unrelated robot must not win
    _ingest([_status("s_other", ts="2026-11-06T19:00:00Z", dog_id="dog_t1", battery=1)], device_id="robot_other")
    owner = Actor(kind="app", id="user_t1", user_id="user_t1")
    st = asyncio.run(routes.get_dog_status("dog_t1", actor=owner))
    assert st["battery"] == 90 and st["last_status"]["docked"] is True
    assert st["device_id"] == "robot_t" and st["last_status_ts"].startswith("2026-11-06T18:00:00")
