"""Replay virtual mode (2026-11-19): while a demo replay is loaded, the app sees the replay's mode
(silent_guardian between the seed's mode start and exit) in GET /status, in robot status frames
forwarded to the owner's apps, and as mode_changed frames when it changes."""
import asyncio
import json

import pytest

import app.database as db
from app.auth import Actor
from app.connection_manager import _replay_overlay, get_connection_manager
from app.routers import event_store as routes
from app.services import push_service, replayer as rp

ADMIN = Actor(kind="admin", id="user_t1", user_id="user_t1")
APP = Actor(kind="app", id="user_t1", user_id="user_t1")

SEED = {
    "version": 1, "name": "mode test", "timezone": "UTC", "day_start": "07:00",
    "events": [
        {"offset_s": 0, "type": "status", "data": {"battery": 98, "mode": "idle", "treats_remaining": 15}},
        {"offset_s": 7200, "type": "status", "data": {"battery": 97, "mode": "silent_guardian", "treats_remaining": 13}},
        {"offset_s": 7500, "type": "mode_change", "data": {"mode": "silent_guardian"}},
        {"offset_s": 9300, "type": "settle", "data": {"kind": "quiet_check", "quiet_min": 30}},
        {"offset_s": 38400, "type": "mode_change", "data": {"mode": "idle"}},
        {"offset_s": 38460, "type": "status", "data": {"battery": 82, "mode": "idle", "treats_remaining": 10}},
    ],
}


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def replay(temp_db, tmp_path, monkeypatch):
    (tmp_path / "mode.json").write_text(json.dumps(SEED))
    monkeypatch.setattr(rp, "seeds_dir", lambda: tmp_path)

    async def fake_push(**kw):
        return "sent"

    monkeypatch.setattr(push_service, "send_push", fake_push)
    db.create_device_pairing("user_t1", "robot_a")
    db.create_device_pairing("user_t1", "robot_b")
    sent = []

    async def fake_send(user_id, msg):
        sent.append(msg)
        return 1

    monkeypatch.setattr(get_connection_manager(), "send_to_user_apps", fake_send)
    r = rp.Replayer()
    monkeypatch.setattr(rp, "_replayer", r)
    run(r.load("mode.json", ADMIN, dog_id="dog_t1", device_id="robot_a", date="2026-11-03"))
    return r, sent


def _modes(sent):
    return [(m["device_id"], m["mode"]) for m in sent if m.get("event") == "mode_changed"]


def test_virtual_mode_follows_the_replay_clock(replay):
    r, sent = replay
    assert r.virtual_mode() is None                   # nothing emitted yet
    run(r.jump(to="08:00"))
    assert r.virtual_mode() == "idle"
    run(r.jump(to="10:00"))                           # after the 09:00 mode start
    assert r.virtual_mode() == "silent_guardian" and r.state()["virtual_mode"] == "silent_guardian"
    run(r.jump(to="18:00"))                           # after the 17:40 exit
    assert r.virtual_mode() == "idle"
    run(r.jump(to="10:00"))                           # back again
    assert r.virtual_mode() == "silent_guardian"
    # one mode_changed per robot of the dog, only when the mode changes
    assert _modes(sent) == [("robot_a", "idle"), ("robot_b", "idle"),
                            ("robot_a", "silent_guardian"), ("robot_b", "silent_guardian"),
                            ("robot_a", "idle"), ("robot_b", "idle"),
                            ("robot_a", "silent_guardian"), ("robot_b", "silent_guardian")]
    assert all(m["reason"] == "replay" for m in sent if m.get("event") == "mode_changed")


def test_status_route_and_live_frames_carry_replay_mode(replay):
    r, _ = replay
    # a live robot keeps saying idle, newer than anything on the replay clock
    run(__import__("app.services.event_store", fromlist=["x"]).ingest_events(
        [{"event_id": "live1", "type": "status", "ts": "2026-11-19T00:00:00Z",
          "data": {"battery": 55, "mode": "idle"}}],
        device_id="robot_b", user_id="user_t1", source="robot", notify=False, forward=False))
    run(r.jump(to="10:00"))
    st = run(routes.get_dog_status("dog_t1", actor=APP))
    assert st["mode"] == "silent_guardian" and st["mode_source"] == "replay" and st["live_mode"] == "idle"
    frame = {"type": "event", "event": "status", "device_id": "robot_b", "mode": "idle",
             "data": {"mode": "idle", "battery": 55}}
    out = _replay_overlay("user_t1", frame)
    assert out["mode"] == "silent_guardian" and out["data"]["mode"] == "silent_guardian"
    assert out["data"]["battery"] == 55 and out["live_mode"] == "idle" and out["mode_source"] == "replay"
    assert frame["mode"] == "idle"                    # the original (stored) frame is untouched
    assert _replay_overlay("someone_else", frame) is frame   # other owners' robots are left alone


def test_no_replay_no_override(replay):
    r, _ = replay
    run(r.reset_dog("dog_t1", ADMIN))
    frame = {"type": "event", "event": "status", "mode": "idle"}
    assert _replay_overlay("user_t1", frame) is frame
    st = run(routes.get_dog_status("dog_t1", actor=APP))
    assert st["mode_source"] == "robot"


# ---- "Barking down X% vs last week" + baseline week landed on load ----

@pytest.fixture()
def real_seeds(temp_db, monkeypatch):
    async def fake_push(**kw):
        return "sent"

    monkeypatch.setattr(push_service, "send_push", fake_push)

    async def fake_send(user_id, msg):
        return 1

    monkeypatch.setattr(get_connection_manager(), "send_to_user_apps", fake_send)
    db.create_device_pairing("user_t1", "robot_a")
    r = rp.Replayer()
    monkeypatch.setattr(rp, "_replayer", r)
    return r


@pytest.mark.parametrize("seed,low,high", [("demo_good_day.json", -99, -90), ("demo_bad_day.json", -60, -45)])
def test_day_seed_lands_last_week_and_card_line(real_seeds, seed, low, high):
    from app.services import event_store
    r = real_seeds
    st = run(r.load(seed, ADMIN, dog_id="dog_t1", device_id="robot_a", date="2026-11-18"))
    assert st["baseline_seed"] == "demo_baseline_week.json" and st["baseline_landed"] > 300
    assert st["baseline_dates"] == ["2026-11-05", "2026-11-11"]          # today-13 .. today-7
    run(r.jump(to="23:00"))
    cmp = event_store.bark_vs_last_week("dog_t1", r.virtual_time())
    assert cmp["last_week"] == {"from": "2026-11-05", "to": "2026-11-11"}
    assert low <= cmp["pct_change"] <= high and cmp["line"] == f"Barking down {-cmp['pct_change']}% vs last week"
    status = run(routes.get_dog_status("dog_t1", actor=APP))
    assert status["bark_vs_last_week"]["line"] == cmp["line"]
    # loading again replaces the baseline rather than doubling it
    st2 = run(r.load(seed, ADMIN, dog_id="dog_t1", device_id="robot_a", date="2026-11-18"))
    assert st2["baseline_landed"] == st["baseline_landed"]


def test_card_line_matches_time_of_day_and_hides_without_last_week(real_seeds):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import event_store
    r = real_seeds
    tz = ZoneInfo("America/New_York")
    assert event_store.bark_vs_last_week("dog_t1", datetime(2026, 11, 18, 12, tzinfo=tz)) is None   # no data
    run(r.load("demo_bad_day.json", ADMIN, dog_id="dog_t1", device_id="robot_a", date="2026-11-18"))
    noon = event_store.bark_vs_last_week("dog_t1", datetime(2026, 11, 18, 12, tzinfo=tz))
    late = event_store.bark_vs_last_week("dog_t1", datetime(2026, 11, 18, 23, tzinfo=tz))
    assert noon["last_week_avg_s"] < late["last_week_avg_s"]           # only last week's barking before noon
    assert noon["today_s"] == 0                                         # replay not started: nothing today yet
