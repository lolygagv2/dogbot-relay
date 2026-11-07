"""Contract fix 2026-11-06: status events are device-scoped.

dog_id is optional for type "status" only; a dog-less status is stored once with dog_id NULL,
keyed by device_id (no per-dog copies).
The dog status route reads the latest status by the paired robot's device_id, not by dog_id.
"""
import asyncio

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


def test_dog_id_required_for_non_status_unless_robot_has_one_dog(temp_db):
    assert EventIn.model_validate(_status("s0")).dog_id is None
    # validate_event (replayer/admin/internal paths) still requires it for non-status
    assert event_store.validate_event({"event_id": "t2", "type": "treat", "ts": "2026-11-06T18:00:00Z",
                                       "data": {"source": "auto"}}) == (None, "dog_id required")
    # a robot with two paired dogs must say which dog
    db.create_device_pairing("user_t1", "robot_t")
    db.create_dog(dog_id="dog_t3", name="Bea", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")
    db.set_device_dogs("robot_t", ["dog_t1", "dog_t3"])
    robot = Actor(kind="robot", id="robot_t", user_id="user_t1", device_id="robot_t")
    batch = EventBatchIn.model_validate({"events": [{"event_id": "t0", "type": "treat", "ts": "2026-11-06T18:00:00Z",
                                                     "data": {"source": "auto"}}]})
    res = asyncio.run(routes.post_events(batch, actor=robot))
    assert res["accepted"] == [] and res["rejected"][0]["reason"].startswith("dog_id required")


def test_status_without_dog_stored_once_with_null_dog(temp_db):
    res = _ingest([_status("s_unpaired")], device_id="robot_lonely")
    assert res["accepted"] == ["s_unpaired"]
    row = db.get_event("s_unpaired")
    assert row["dog_id"] is None and row["device_id"] == "robot_lonely"
    assert db.get_latest_status_for_devices(["robot_lonely"])["event_id"] == "s_unpaired"
    assert _ingest([_status("s_unpaired")], device_id="robot_lonely")["duplicate"] == ["s_unpaired"]


def test_status_is_one_row_even_with_several_paired_dogs(temp_db):
    db.create_device_pairing("user_t1", "robot_t")
    db.create_dog(dog_id="dog_t3", name="Bea", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t3", "owner")
    res = _ingest([_status("s_multi")])
    assert res["accepted"] == ["s_multi"]
    assert db.get_event("s_multi")["dog_id"] is None
    assert db.get_event("s_multi:dog_t3") is None
    with db.db_connection() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE type = 'status'").fetchone()[0] == 1
    assert _ingest([_status("s_multi")])["duplicate"] == ["s_multi"]
    # both dogs see it through the shared robot
    owner = Actor(kind="app", id="user_t1", user_id="user_t1")
    for dog in ("dog_t1", "dog_t3"):
        assert asyncio.run(routes.get_dog_status(dog, actor=owner))["battery"] == 77


def test_rebuild_migration_backs_up_first(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE events (event_id TEXT PRIMARY KEY, dog_id TEXT NOT NULL, device_id TEXT NOT NULL,
        user_id TEXT, type TEXT NOT NULL, ts TEXT NOT NULL, duration_s REAL, data TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'robot', local_date TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO events VALUES ('e1','dog_t1','r','u','treat','t',NULL,'{}','robot','d','c')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    backups = list(tmp_path.glob("old.db.bak-*-events_dog_id_nullable"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert b.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert [c[3] for c in b.execute("PRAGMA table_info(events)") if c[1] == "dog_id"] == [1]  # pre-rebuild copy
    b.close()
    assert db.get_event("e1")["dog_id"] == "dog_t1"  # data survived the rebuild
    db.init_db()  # already nullable: no second backup
    assert len(list(tmp_path.glob("old.db.bak-*"))) == 1


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
