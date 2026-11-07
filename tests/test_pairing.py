"""Owner model 2026-11-07: owners, robots and dogs are independent.

A robot belongs to an owner, a dog belongs to an owner, and robots and dogs are joined only
through that owner. Robot state (online, battery) never depends on which dog is asked about.
A robot is never blocked: an owner with no dogs gets a placeholder dog.
"""
import asyncio

import pytest
from fastapi import HTTPException

import app.database as db
from app.auth import Actor
from app.connection_manager import get_connection_manager
from app.models import EventBatchIn
from app.routers import event_store as routes
from app.routers import user as user_routes
from app.services import event_store, pairing, robot_control

ROBOT = Actor(kind="robot", id="wimz_robot_05", user_id="user_t1", device_id="wimz_robot_05")
OWNER = Actor(kind="app", id="user_t1", user_id="user_t1")
PH = "ph_user_t1_wimz_robot_05"


def _post(events, actor=ROBOT):
    return asyncio.run(routes.post_events(EventBatchIn.model_validate({"events": events}), actor=actor))


def _second_dog():
    db.create_dog(dog_id="dog_t3", name="Bea", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")


def _owner_without_dogs():
    db.delete_dog("dog_t1")  # user_t1 now has no dogs
    db.create_device_pairing("user_t1", "wimz_robot_05")


@pytest.fixture
def robot_online():
    """Mark devices as connected in the (process-global) connection manager, then clean up."""
    manager = get_connection_manager()
    added = []

    def _online(device_id):
        manager.robot_connections[device_id] = object()
        added.append(device_id)

    yield _online
    for d in added:
        manager.robot_connections.pop(d, None)


def test_placeholder_name():
    assert pairing.placeholder_name("wimz_robot_05") == "Robot 05's dog"
    assert pairing.placeholder_name("garage-bot") == "garage-bot's dog"


def test_unowned_robot_gets_no_dog(temp_db):
    assert pairing.device_dog_ids("wimz_robot_99") == []


def test_robot_serves_all_owner_dogs_and_dog_has_all_owner_robots(temp_db):
    _second_dog()
    db.create_device_pairing("user_t1", "wimz_robot_05")
    db.create_device_pairing("user_t1", "wimz_robot_06")
    assert pairing.device_dog_ids("wimz_robot_05") == ["dog_t1", "dog_t3"]
    assert pairing.device_dog_ids("wimz_robot_06") == ["dog_t1", "dog_t3"]
    assert sorted(pairing.dog_devices("dog_t3")) == ["wimz_robot_05", "wimz_robot_06"]
    assert db.get_dog_by_id(PH) is None                          # no placeholder
    # a dog added later is served immediately, with nothing to link
    db.create_dog(dog_id="dog_t4", name="Cy", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t4", "owner")
    assert pairing.device_dog_ids("wimz_robot_05") == ["dog_t1", "dog_t3", "dog_t4"]


def test_viewer_dogs_are_not_the_owners_dogs(temp_db):
    db.create_device_pairing("user_t2", "wimz_robot_06")         # user_t2 only views dog_t1
    assert pairing.device_dog_ids("wimz_robot_06") == ["ph_user_t2_wimz_robot_06"]


def test_online_is_the_robots_state_for_every_dog(temp_db, robot_online):
    """Regression 2026-11-07: robot linked (v1) only to dog B showed offline for dog A."""
    _second_dog()
    db.create_device_pairing("user_t1", "wimz_robot_05")
    db.create_device_pairing("user_t1", "wimz_robot_02")         # an old, disconnected robot
    db.set_device_dogs("wimz_robot_05", ["dog_t3"])              # stale v1 link: ignored now
    db.set_device_dogs("wimz_robot_02", ["dog_t1"])
    robot_online("wimz_robot_05")
    for dog in ("dog_t1", "dog_t3"):
        st = asyncio.run(routes.get_dog_status(dog, actor=OWNER))
        assert st["robot_online"] is True, dog
        # commands for either dog go to the connected robot
        assert robot_control._pick_device("user_t1", None, dog) == ("wimz_robot_05", True)


def test_offline_when_none_of_the_owners_robots_is_connected(temp_db):
    db.create_device_pairing("user_t1", "wimz_robot_05")
    st = asyncio.run(routes.get_dog_status("dog_t1", actor=OWNER))
    assert st["robot_online"] is False and st["device_id"] == "wimz_robot_05"


def test_dog_id_inferred_only_when_owner_has_one_dog(temp_db):
    db.create_device_pairing("user_t1", "wimz_robot_05")
    treat = {"event_id": "t1", "type": "treat", "ts": "2026-11-06T15:00:00Z", "data": {"source": "auto"}}
    assert _post([treat])["accepted"] == ["t1"] and db.get_event("t1")["dog_id"] == "dog_t1"
    _second_dog()
    res = _post([{**treat, "event_id": "t2"}])
    assert res["accepted"] == [] and res["rejected"][0]["reason"] == "dog_id required (owner has 2 dogs)"
    assert _post([{**treat, "event_id": "t3", "dog_id": "dog_t3"}])["accepted"] == ["t3"]


def test_placeholder_created_once(temp_db):
    _owner_without_dogs()
    assert pairing.device_dog_ids("wimz_robot_05") == [PH]
    dog = db.get_dog_by_id(PH)
    assert dog["name"] == "Robot 05's dog" and dog["is_placeholder"] is True
    assert db.get_dog_owner(PH) == "user_t1" and db.get_user_dog_role("user_t1", PH) == "owner"
    assert pairing.device_dog_ids("wimz_robot_05") == [PH]  # idempotent
    assert db.get_dog_count() == 1
    # a second robot for the same owner reuses the owner's (placeholder) dog
    db.create_device_pairing("user_t1", "wimz_robot_06")
    assert pairing.device_dog_ids("wimz_robot_06") == [PH]


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
    assert robot_control._pick_device("user_t1", None, PH)[0] == "wimz_robot_05"
    assert pairing.primary_dog_id("wimz_robot_05") == PH
    from app.voice_agent.events import _pick_dog
    assert _pick_dog("user_t1", None, "wimz_robot_05")["id"] == PH


def test_rename_keeps_events_and_merge_moves_them(temp_db):
    _owner_without_dogs()
    _post([{"event_id": "t1", "type": "treat", "ts": "2026-11-06T15:00:00Z", "data": {"source": "auto"}}])
    db.update_dog(PH, name="Bezik")
    assert db.get_dog_by_id(PH)["is_placeholder"] is False and db.get_event("t1")["dog_id"] == PH
    db.create_dog(dog_id="dog_real", name="Rex", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_real", "owner")
    db.merge_dogs("user_t1", "dog_real", [PH])
    assert db.get_event("t1")["dog_id"] == "dog_real" and db.get_dog_by_id(PH) is None
    assert event_store.get_summary("dog_real", "2026-11-06")["totals"]["treats"] == 1


def test_device_dogs_endpoint_lists_owner_dogs_and_put_is_gone(temp_db):
    _second_dog()
    db.create_device_pairing("user_t1", "wimz_robot_05")
    me = {"user_id": "user_t1"}
    out = asyncio.run(user_routes.get_device_dogs("wimz_robot_05", current_user=me))
    assert [d["dog_id"] for d in out["dogs"]] == ["dog_t1", "dog_t3"]
    with pytest.raises(HTTPException) as e:
        asyncio.run(user_routes.put_device_dogs("wimz_robot_05", current_user=me))
    assert e.value.status_code == 410
    with pytest.raises(HTTPException) as e:                      # not your robot
        asyncio.run(user_routes.get_device_dogs("wimz_robot_05", current_user={"user_id": "user_t2"}))
    assert e.value.status_code == 404


def test_robot_handed_to_new_owner_serves_only_new_owners_dogs(temp_db):
    db.create_device_pairing("user_t1", "wimz_robot_05")
    assert pairing.device_dog_ids("wimz_robot_05") == ["dog_t1"]
    db.create_device_pairing("user_t2", "wimz_robot_05")
    assert pairing.device_dog_ids("wimz_robot_05") == ["ph_user_t2_wimz_robot_05"]
