"""music_plan triggers, set_music, missions without departure, seeds on mode start/exit (2026-11-18)."""
import asyncio
import inspect
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models import MissionTrigger, MusicPlan, NotifyRule
from app.services import event_store, notify_service, settings_service, settings_tools
from tests.test_settings_tools import LocalClient

SEEDS = Path(__file__).parent.parent / "seeds"


def run(c):
    return asyncio.run(c)


def music(**kw):
    return run(settings_tools.set_music(LocalClient(), "dog_t1", **kw))


def plan():
    return settings_service.get_value("dog_t1", "music_plan")


# ---- model ----

def test_default_plan_has_no_triggers():
    p = MusicPlan().model_dump()
    assert p == {"enabled": True, "volume": 40, "on_mode_start": None, "after_treat": None,
                 "on_bark_episode": None, "at_time": []}


def test_legacy_plan_maps_schedule_and_drops_departure():
    p = MusicPlan.model_validate({"volume": 30, "on_departure": {"track": "song_calm_01", "delay_s": 60},
                                  "schedule": [{"time": "13:00", "track": "song_calm_02", "duration_min": 20}]})
    d = p.model_dump()
    assert "on_departure" not in d and d["at_time"] == [{"track": "song_calm_02", "duration_min": 20, "time": "13:00"}]


def test_departure_is_no_longer_a_trigger_anywhere():
    with pytest.raises(ValueError):
        MissionTrigger(kind="departure")
    with pytest.raises(HTTPException) as e:
        settings_service.validate_value("missions", {"missions": [
            {"id": "m1", "type": "recall", "trigger": {"kind": "departure"}}]})
    assert e.value.status_code == 422
    with pytest.raises(ValueError):
        NotifyRule(id="r", event_type="departure")
    assert "departure" not in settings_tools.NOTIFY_EVENT_TYPES and "arrival" not in settings_tools.NOTIFY_EVENT_TYPES
    rules = settings_service.default_value("dog_t1", "notify_rules")["rules"]
    assert all(r["event_type"] not in ("departure", "arrival") for r in rules)
    for name in ("handle_triggers", "arm_departure_fallbacks", "cancel_departure_triggers", "DEPARTURE_FALLBACK_S"):
        assert not hasattr(notify_service, name)


# ---- set_music ----

def test_each_trigger_can_be_set(temp_db):
    r = music(trigger="on_mode_start", mode="coach", delay_s=0, track="song_calm_02", duration_min=15)
    assert r["ok"] and r["music_plan"]["on_mode_start"] == {"track": "song_calm_02", "duration_min": 15,
                                                            "mode": "coach", "delay_s": 0}
    r = music(trigger="after_treat", every_n=3, minutes=10)
    assert r["music_plan"]["after_treat"] == {"track": "song_calm_01", "duration_min": 20, "every_n": 3, "minutes": 10}
    r = music(trigger="on_bark_episode", min_severity="high", duration_min=30)
    assert r["music_plan"]["on_bark_episode"]["min_severity"] == "high"
    r = music(trigger="at_time", time="13:00", track="song_calm_02")
    assert r["music_plan"]["at_time"] == [{"track": "song_calm_02", "duration_min": 20, "time": "13:00"}]
    # the earlier triggers are kept
    assert plan()["on_mode_start"]["mode"] == "coach" and plan()["after_treat"]["every_n"] == 3


def test_partial_update_keeps_other_fields_and_volume_keeps_triggers(temp_db):
    music(trigger="after_treat", every_n=4, minutes=15, track="song_calm_02")
    r = music(trigger="after_treat", minutes=5)
    assert r["music_plan"]["after_treat"] == {"track": "song_calm_02", "duration_min": 20, "every_n": 4, "minutes": 5}
    r = music(volume=55)
    assert r["volume"] == 55 and r["music_enabled"] is True and r["music_plan"]["after_treat"]["every_n"] == 4
    r = music(enabled=False)
    assert r["music_enabled"] is False and r["volume"] == 55


def test_at_time_slots_upsert_and_remove(temp_db):
    music(trigger="at_time", time="18:00")
    music(trigger="at_time", time="13:00")
    music(trigger="at_time", time="13:00", duration_min=45)
    assert [(s["time"], s["duration_min"]) for s in plan()["at_time"]] == [("13:00", 45), ("18:00", 20)]
    music(trigger="at_time", remove=True, time="13:00")
    assert [s["time"] for s in plan()["at_time"]] == ["18:00"]
    music(trigger="at_time", remove=True)
    assert plan()["at_time"] == []


def test_remove_switches_a_trigger_off(temp_db):
    music(trigger="on_bark_episode", min_severity="low")
    r = music(trigger="on_bark_episode", remove=True)
    assert r["ok"] and r["music_plan"]["on_bark_episode"] is None


@pytest.mark.parametrize("kw,reason", [
    ({}, "nothing_to_change"),
    ({"trigger": "on_departure"}, "invalid_trigger"),
    ({"every_n": 3}, "trigger_required"),
    ({"trigger": "after_treat", "mode": "coach"}, "field_not_for_trigger"),
    ({"trigger": "on_mode_start", "track": "song_nope"}, "unknown_track"),
    ({"trigger": "after_treat", "every_n": 0}, "invalid_value"),
    ({"trigger": "on_mode_start", "mode": "idle"}, "invalid_value"),
    ({"trigger": "on_bark_episode", "min_severity": "extreme"}, "invalid_value"),
    ({"trigger": "at_time"}, "invalid_time"),
    ({"trigger": "at_time", "time": "25:00"}, "invalid_time"),
    ({"volume": 101}, "invalid_volume"),
])
def test_refusals(temp_db, kw, reason):
    r = music(**kw)
    assert r["refused"] and r["reason"] == reason
    assert settings_service.get_setting("dog_t1", "music_plan")["version"] == 0   # nothing saved


def test_mcp_and_voice_tools_expose_the_triggers():
    from app.mcp_server import tools as mt
    from app.voice_agent import tools as vt
    src = inspect.getsource(mt)
    for name in ("on_mode_start", "after_treat", "on_bark_episode", "at_time", "every_n", "min_severity"):
        assert name in src
    assert "on_departure" not in src and '"alone_s"' not in src and '"departure"' not in src
    props = {t["name"]: t for t in vt.TOOLS}["set_music"]["input_schema"]["properties"]
    assert props["trigger"]["enum"] == ["on_mode_start", "after_treat", "on_bark_episode", "at_time"]
    assert {"mode", "delay_s", "every_n", "minutes", "min_severity", "time", "track", "duration_min"} <= set(props)


# ---- roll-up ----

def test_summary_reports_sg_s_and_no_alone_s(temp_db):
    s = event_store.get_summary("dog_t1", "2026-11-06", refresh=True)
    assert "sg_s" in s["totals"] and "alone_s" not in s["totals"]
    assert "departure" not in s["counts"] and "arrival" not in s["counts"]


# ---- seeds ----

@pytest.mark.parametrize("path", sorted(SEEDS.glob("*.json")), ids=lambda p: p.name)
def test_seeds_use_mode_start_and_exit(path):
    seed = json.loads(path.read_text())
    types = {e["type"] for e in seed["events"]}
    assert not types & {"departure", "arrival"}
    assert "mode_change" in types
    assert "departure" not in json.dumps(seed) and "on_departure" not in json.dumps(seed)
    for key, value in (seed.get("settings") or {}).items():
        settings_service.validate_value(key, value)     # raises on a bad shape
    for i, e in enumerate(seed["events"]):
        ev, reason = event_store.validate_event({"event_id": f"x{i}", "dog_id": "dog_t1", "ts": "2026-11-06T12:00:00Z",
                                                 "type": e["type"], "duration_s": e.get("duration_s"),
                                                 "data": dict(e.get("data") or {})})
        assert ev is not None, (i, reason)
