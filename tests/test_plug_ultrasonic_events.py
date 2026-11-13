"""Plug / ultrasonic robot events are readable as typed events (2026-11-13).

On 2026-11-12 the robot reported the diffuser plug switching on (sg_level, level 2), but the event
sat only in activity_events. The assistants' get_events reads the typed `events` table, so the
assistant saw no plug event and told the owner the plug had failed. These events are now copied
into `events`, once per dog the robot serves.
"""
import asyncio

import app.database as db
from app.auth import Actor
from app.mcp_server import tools as mcp_tools
from app.models import EVENT_TYPES
from app.routers import event_store as routes
from app.routers import websocket as ws
from app.services import event_store

OWNER = Actor(kind="app", id="user_t1", user_id="user_t1")
DEV = "wimz_robot_05"

PLUG_ON = {
    "type": "smart_plug", "on": True, "reason": "sg_level", "alias": "Wall bearer", "owner": "robot",
    "auto_off_at": 1794530059.2, "level": 2, "event_id": "plug-1", "timestamp": "2026-11-12T22:34:19.763Z",
}


def _setup(two_dogs=False):
    db.create_device_pairing("user_t1", DEV)
    if two_dogs:
        db.create_dog(dog_id="dog_t3", name="Bea", user_id="user_t1")
        db.add_user_dog("user_t1", "dog_t3", "owner")


def _ws_event(msg):
    msg = dict(msg)
    ws._normalize_robot_event(msg)

    async def run():  # the push step needs a running loop (asyncio.create_task)
        ws._maybe_persist_as_activity(DEV, "user_t1", msg)

    asyncio.run(run())


def _events(dog_id, type=None):
    return asyncio.run(routes.get_dog_events(dog_id, since=None, until=None, type=type, limit=100,
                                             order="desc", actor=OWNER))["events"]


def test_types_known_everywhere():
    for t in ("smart_plug", "ultrasonic"):
        assert t in EVENT_TYPES and t in mcp_tools.EVENT_TYPES
        assert ws.LEGACY_TO_ACTIVITY_TYPE[t] == t


def test_plug_event_lands_in_typed_events(temp_db):
    _setup()
    _ws_event(PLUG_ON)
    rows = _events("dog_t1", "smart_plug")
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "smart_plug" and r["source"] == "robot" and r["device_id"] == DEV
    assert r["data"]["on"] is True and r["data"]["reason"] == "sg_level" and r["data"]["level"] == 2
    assert r["ts"].startswith("2026-11-12T22:34:19")
    # visible with no type filter too (the assistant's default call)
    assert any(e["type"] == "smart_plug" for e in _events("dog_t1"))


def test_one_row_per_dog_and_replay_is_duplicate(temp_db):
    _setup(two_dogs=True)
    _ws_event(PLUG_ON)
    _ws_event(PLUG_ON)  # replayed frame, same event_id
    assert len(_events("dog_t1", "smart_plug")) == 1
    assert len(_events("dog_t3", "smart_plug")) == 1


def test_ultrasonic_from_type_only_frame(temp_db):
    _setup()
    _ws_event({"type": "ultrasonic", "cause": "sg", "sg_level": 3, "episode_id": "ep1", "freq_hz": 25000,
               "duration_s": 2, "event_id": "us-1", "timestamp": "2026-11-12T23:16:37Z"})
    rows = _events("dog_t1", "ultrasonic")
    assert len(rows) == 1 and rows[0]["data"]["sg_level"] == 3 and rows[0]["data"]["episode_id"] == "ep1"


def test_day_summary_counts_plug(temp_db):
    _setup()
    _ws_event(PLUG_ON)
    local_date = db.list_events("dog_t1", types=["smart_plug"])[0]["local_date"]
    summary = event_store.compute_daily_summary("dog_t1", local_date)
    assert summary["counts"]["smart_plug"] == 1
    assert summary["counts"]["ultrasonic"] == 0


def test_unowned_robot_writes_nothing(temp_db):
    assert event_store.record_ws_device_event("wimz_robot_99", None, "smart_plug", "x", None, {}) == 0
    assert event_store.record_ws_device_event(DEV, "user_t1", "bark", "x", None, {}) == 0
