"""Replay jump catch-up: a forward jump fires the newest skipped push per rule, the last SG
re-arm (if still armed) and low hopper (if crossed), instead of silence (2026-11-08)."""
import asyncio
import json

import pytest

from app.auth import Actor
from app.services import event_store, notify_service, push_service, replayer as rp

ADMIN = Actor(kind="admin", id="user_t1", user_id="user_t1")
DEVICE = "robot_demo"

SEED = {
    "version": 1, "name": "jump test", "timezone": "UTC", "day_start": "07:00",
    "events": [
        {"offset_s": 0, "type": "status", "data": {"battery": 90, "mode": "idle", "treats_remaining": 8}},
        {"offset_s": 600, "type": "bark_episode", "duration_s": 90, "id": "ep1",
         "data": {"bark_count": 20, "severity": "high", "image_url": "/api/media/download/seed_a.png"}},
        {"offset_s": 1200, "type": "bark_episode", "duration_s": 120, "id": "ep2",
         "data": {"bark_count": 30, "severity": "med", "image_url": "/api/media/download/seed_b.png"}},
        {"offset_s": 1300, "type": "settle", "data": {"episode_ref": "ep2"}},
        {"offset_s": 1500, "type": "status", "data": {"battery": 88, "mode": "silent_guardian", "treats_remaining": 5}},
        {"offset_s": 1800, "type": "status", "data": {"battery": 87, "mode": "silent_guardian", "treats_remaining": 2}},
        {"offset_s": 2400, "type": "bark_episode", "duration_s": 10, "data": {"bark_count": 3, "severity": "low"}},
        {"offset_s": 5000, "type": "status", "data": {"battery": 80, "mode": "idle", "treats_remaining": 2}},
    ],
}


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def rec(temp_db, tmp_path, monkeypatch):
    (tmp_path / "jump.json").write_text(json.dumps(SEED))
    monkeypatch.setattr(rp, "seeds_dir", lambda: tmp_path)
    pushes, hooks = [], []

    async def fake_push(**kw):
        pushes.append(kw)
        return "sent"

    monkeypatch.setattr(push_service, "send_push", fake_push)
    monkeypatch.setattr(notify_service, "notify_agent",
                        lambda event, device_id, user_id, dog_id, payload=None, event_id=None:
                        hooks.append({"event": event, "device_id": device_id, "payload": payload, "event_id": event_id}))
    event_store._last_mode.pop(DEVICE, None)
    r = rp.Replayer()

    async def go(*offsets):
        await r.load("jump.json", ADMIN, dog_id="dog_t1", device_id=DEVICE, date="2026-11-03")
        return [await r.jump(offset_s=o) for o in offsets]

    return r, go, pushes, hooks


def test_forward_jump_lands_with_newest_push_rearm_and_low_hopper(rec):
    r, go, pushes, hooks = rec
    (state,) = run(go(3000))
    assert state["cursor"] == 7
    # bark_long (>=60 s, med/high): one push, for the newest matching episode, with its still
    barks = [p for p in pushes if p["app_type"] == "bark"]
    assert len(barks) == 1 and barks[0]["data"]["event_id"] == "replay:dog_t1:2026-11-03:2"
    assert barks[0]["data"]["image_url"] == "/api/media/download/seed_b.png" and barks[0]["image_url"].endswith("/api/media/download/seed_b.png")
    settled = [p for p in pushes if p["title"].endswith("settled")]
    assert len(settled) == 1 and len(pushes) == 2
    assert [h["event"] for h in hooks] == ["sg_rearm", "low_hopper"]
    assert hooks[0]["payload"]["previous_mode"] == "idle" and hooks[0]["event_id"].endswith(":4")
    assert hooks[1]["payload"]["treats_remaining"] == 2 and hooks[1]["device_id"] == DEVICE
    assert {c["kind"] for c in state["catch_up"]} == {"notify", "sg_rearm", "low_hopper"}
    assert event_store._last_mode[DEVICE] == "silent_guardian"


def test_no_rearm_when_disarmed_by_landing(rec):
    r, go, pushes, hooks = rec
    (state,) = run(go(6000))
    assert [h["event"] for h in hooks] == ["low_hopper"]
    assert event_store._last_mode[DEVICE] == "idle"


def test_no_low_hopper_without_a_crossing(rec):
    r, go, pushes, hooks = rec
    run(go(1900))                    # crosses 8 -> 2 here
    hooks.clear()
    s = run(r.jump(offset_s=6000))   # already low before this jump: no new crossing
    assert "low_hopper" not in [h["event"] for h in hooks]
    assert [c["kind"] for c in s["catch_up"]] == []                    # 10 s low bark matches no rule


def test_backward_jump_resets_mode_and_fires_nothing(rec):
    r, go, pushes, hooks = rec
    run(go(3000))
    pushes.clear(), hooks.clear()
    s = run(r.jump(offset_s=100))
    assert s["catch_up"] == [] and not pushes and not hooks
    assert event_store._last_mode[DEVICE] == "idle"


def test_small_forward_jump_with_nothing_qualifying_is_quiet(rec):
    r, go, pushes, hooks = rec
    (s,) = run(go(300))              # only the first status
    assert s["catch_up"] == [] and not pushes and not hooks
    assert event_store._last_mode[DEVICE] == "idle"


def test_cooldown_runs_on_the_replay_clock(rec, tmp_path):
    """bark_long has a 900 s cooldown: judged by event time, not by how fast the jumps come."""
    r, go, pushes, hooks = rec
    long_bark = lambda t, n: {"offset_s": t, "type": "bark_episode", "duration_s": 90,
                              "data": {"bark_count": n, "severity": "high"}}
    seed = dict(SEED, events=[SEED["events"][0], long_bark(600, 1), long_bark(1200, 2), long_bark(3000, 3)])
    (tmp_path / "cool.json").write_text(json.dumps(seed))

    async def go_cool():
        await r.load("cool.json", ADMIN, dog_id="dog_t1", device_id=DEVICE, date="2026-11-03")
        out = []
        for o in (700, 1300, 3100):          # three jumps within the same wall-clock second
            pushes.clear()
            await r.jump(offset_s=o)
            out.append([p["data"]["event_id"] for p in pushes if p["app_type"] == "bark"])
        return out

    first, second, third = run(go_cool())
    assert first == ["replay:dog_t1:2026-11-03:1"]
    assert second == []                                # 600 s of replay time after the last push
    assert third == ["replay:dog_t1:2026-11-03:3"]     # 2400 s later on the replay clock


def test_live_events_keep_wall_clock_cooldown(temp_db, monkeypatch):
    import app.database as db
    from datetime import datetime, timedelta, timezone
    sent = []

    async def fake_push(**kw):
        sent.append(kw["data"]["event_id"])
        return "sent"

    monkeypatch.setattr(push_service, "send_push", fake_push)
    db.create_device_pairing("user_t1", "robot_t")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    for eid, ts in (("live1", older), ("live2", old)):   # 1 h apart in event time, seconds apart in wall time
        run(event_store.ingest_events([{"event_id": eid, "type": "bark_episode", "dog_id": "dog_t1", "ts": ts,
                                        "duration_s": 90, "data": {"bark_count": 9, "severity": "high"}}],
                                      device_id="robot_t", user_id="user_t1", source="robot", forward=False))
    assert sent == ["live1"]                           # live robot: wall-clock cooldown, as before
