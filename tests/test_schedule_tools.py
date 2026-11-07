"""get_schedule / schedule_mission / cancel_mission + robot schedule_sync (2026-11-07)."""
import asyncio
import inspect

import pytest
from pydantic import ValidationError

from app.auth import Actor
from app.mcp_server import authz
from app.models import MissionDef
from app.services import robot_control as rc
from app.services import schedule_tools as st
from app.services.relay_client import RelayRefusal

CONN = Actor(kind="connector", id="claude", user_id="user_t1")


class FakeRelay:
    """Stands in for RelayClient: a missions setting plus a scripted robot schedule_sync reply."""

    def __init__(self, missions=None, robot="echo", conflicts=0):
        self.missions, self.version, self.robot, self.conflicts = list(missions or []), 1, robot, conflicts
        self.puts, self.commands = [], []

    async def get_setting(self, dog_id, key):
        assert key == "missions"
        return {"value": {"missions": [dict(m) for m in self.missions]}, "version": self.version}

    async def put_setting(self, dog_id, key, value, expected_version=None):
        if self.conflicts:
            self.conflicts -= 1
            self.version += 1
            raise RelayRefusal(409, "version_mismatch", {"code": "version_mismatch"})
        assert expected_version == self.version
        self.missions, self.version = value["missions"], self.version + 1
        self.puts.append(value)
        return {"value": value, "version": self.version}

    async def command(self, dog_id, command, data=None, device_id=None, wait_s=None):
        self.commands.append((command, wait_s))
        assert command == "schedule_sync"
        if self.robot == "offline":
            raise RelayRefusal(409, "robot_offline", {"code": "robot_offline"})
        if self.robot == "timeout":
            return {"delivered": True, "timed_out": True, "result": None}
        if self.robot == "stale":   # the robot answers with an empty list
            missions = []
        else:                       # the robot has whatever the relay holds
            missions = [{"id": m["id"], "enabled": True, "next_fire_at": f"2026-11-08T{m['trigger']['time']}:00-05:00"}
                        for m in self.missions]
        return {"delivered": True, "result": {"ok": True, "result": {"missions": missions}}}


def run(coro):
    return asyncio.run(coro)


# ---- model + allowlist ----

def test_mode_missions_may_run_12h_training_1h():
    MissionDef(id="m1", type="silent_guardian", duration_s=12 * 3600)
    MissionDef(id="m2", type="coach", duration_s=4 * 3600)
    MissionDef(id="m3", type="recall", duration_s=3600)
    with pytest.raises(ValidationError):
        MissionDef(id="m4", type="recall", duration_s=3601)
    with pytest.raises(ValidationError):
        MissionDef(id="m5", type="silent_guardian", duration_s=12 * 3600 + 1)


@pytest.mark.parametrize("kind", ["connector", "agent", "app", "admin"])
def test_schedule_sync_is_allowlisted(temp_db, kind):
    msg, _ = rc.build_robot_message("schedule_sync", {}, Actor(kind=kind, id="x", user_id="user_t1"), "dog_t1")
    assert msg["command"] == "schedule_sync" and msg["data"]["dog_id"] == "dog_t1" and msg["command_id"]


def test_connector_may_write_mode_and_training_missions(temp_db):
    from app.services import settings_service
    value = {"missions": [MissionDef(id="m_sg", type="silent_guardian", duration_s=7200).model_dump(),
                          MissionDef(id="m_r", type="recall").model_dump()]}
    row = run(settings_service.put_setting("dog_t1", "missions", value, CONN))
    assert [m["id"] for m in row["value"]["missions"]] == ["m_sg", "m_r"]


# ---- schedule_mission ----

def test_schedule_silent_guardian_confirmed_with_next_fire_at():
    c = FakeRelay()
    r = run(st.schedule_mission(c, "dog_t1", "silent_guardian", "18:00", ["mon", "fri"], 180))
    assert r["ok"] and r["scheduled"] and r["confirmed_by_robot"]
    assert r["next_fire_at"] == "2026-11-08T18:00:00-05:00"
    assert r["type"] == "silent_guardian" and r["duration_min"] == 180 and r["days"] == ["mon", "fri"]
    m = c.missions[0]
    assert m["type"] == "silent_guardian" and m["duration_s"] == 10800 and m["reward_treats"] == 0
    assert m["trigger"]["kind"] == "schedule" and m["trigger"]["time"] == "18:00"
    assert m["name"] == "Silent Guardian at 18:00"
    assert c.commands == [("schedule_sync", st.SYNC_WAIT_S)]
    MissionDef.model_validate(m)


def test_schedule_coach_defaults():
    c = FakeRelay()
    r = run(st.schedule_mission(c, "dog_t1", "coach", "07:30"))
    assert r["confirmed_by_robot"] and r["duration_min"] == 120 and r["days"] == st.DAYS
    assert c.missions[0]["name"] == "Coach mode at 07:30"


def test_schedule_training_mission_defaults():
    c = FakeRelay()
    r = run(st.schedule_mission(c, "dog_t1", "sit_stay", "09:00"))
    assert r["duration_min"] == 2 and c.missions[0]["reward_treats"] == 1
    assert c.missions[0]["name"] == "Sit stay at 09:00"


def test_robot_timeout_saves_but_does_not_confirm():
    c = FakeRelay(robot="timeout")
    r = run(st.schedule_mission(c, "dog_t1", "silent_guardian", "18:00"))
    assert r["ok"] and len(c.missions) == 1
    assert r["confirmed_by_robot"] is False and r["robot_reason"] == "robot_did_not_confirm"
    assert "next_fire_at" not in r and "not confirmed" in r["message"]


def test_robot_offline_saves_but_does_not_confirm():
    r = run(st.schedule_mission(FakeRelay(robot="offline"), "dog_t1", "coach", "18:00"))
    assert r["ok"] and r["confirmed_by_robot"] is False and r["robot_reason"] == "robot_offline"


def test_robot_replied_without_the_mission():
    r = run(st.schedule_mission(FakeRelay(robot="stale"), "dog_t1", "coach", "18:00"))
    assert r["confirmed_by_robot"] is False and r["robot_reason"] == "robot_has_not_loaded_mission"


@pytest.mark.parametrize("args,reason", [
    (("walkies", "18:00"), "unknown_mission_type"),
    (("coach", "6pm"), "time_must_be_HH:MM_24h"),
    (("coach", "24:00"), "time_must_be_HH:MM_24h"),
    (("coach", "18:00", None, 721), "duration_out_of_range"),
    (("silent_guardian", "18:00", None, 4), "duration_out_of_range"),
    (("recall", "18:00", None, 61), "duration_out_of_range"),
])
def test_invalid_requests_refused_without_writing(args, reason):
    c = FakeRelay()
    r = run(st.schedule_mission(c, "dog_t1", *args))
    assert r["refused"] and r["reason"] == reason and not c.puts and not c.commands


def test_same_time_warning_and_version_retry():
    c = FakeRelay(conflicts=1)
    run(st.schedule_mission(c, "dog_t1", "coach", "18:00"))
    r = run(st.schedule_mission(c, "dog_t1", "silent_guardian", "18:00"))
    assert len(c.missions) == 2 and any("18:00" in w for w in r["warnings"])


# ---- cancel_mission ----

def test_cancel_removes_and_robot_confirms():
    c = FakeRelay()
    mid = run(st.schedule_mission(c, "dog_t1", "silent_guardian", "18:00"))["mission_id"]
    run(st.schedule_mission(c, "dog_t1", "coach", "07:00"))
    r = run(st.cancel_mission(c, "dog_t1", mid))
    assert r["ok"] and r["cancelled"] and r["confirmed_by_robot"] and r["name"] == "Silent Guardian at 18:00"
    assert [m["type"] for m in c.missions] == ["coach"]


def test_cancel_unknown_mission_refused():
    c = FakeRelay()
    run(st.schedule_mission(c, "dog_t1", "coach", "07:00"))
    r = run(st.cancel_mission(c, "dog_t1", "m_nope"))
    assert r["refused"] and r["reason"] == "unknown_mission" and len(c.missions) == 1
    assert r["known_mission_ids"] == [c.missions[0]["id"]]


def test_cancel_robot_still_has_it():
    c = FakeRelay()
    mid = run(st.schedule_mission(c, "dog_t1", "coach", "07:00"))["mission_id"]
    stale = [dict(m) for m in c.missions]

    async def still_there(*a, **k):
        return {"delivered": True, "result": {"ok": True, "result": {"missions": [{"id": stale[0]["id"]}]}}}
    c.command = still_there
    r = run(st.cancel_mission(c, "dog_t1", mid))
    assert r["cancelled"] and r["confirmed_by_robot"] is False and r["robot_reason"] == "robot_still_has_mission"


# ---- get_schedule ----

def test_get_schedule_merges_robot_view():
    c = FakeRelay()
    run(st.schedule_mission(c, "dog_t1", "silent_guardian", "18:00"))

    async def robot(*a, **k):
        return {"delivered": True, "result": {"ok": True, "result": {"missions": [
            {"id": c.missions[0]["id"], "enabled": True, "next_fire_at": "2026-11-08T18:00:00-05:00"},
            {"id": "legacy_1", "enabled": True, "next_fire_at": None}]}}}
    c.command = robot
    r = run(st.get_schedule(c, "dog_t1"))
    assert r["confirmed_by_robot"] and r["missions"][0]["on_robot"]
    assert r["missions"][0]["next_fire_at"] == "2026-11-08T18:00:00-05:00"
    assert r["robot_only_mission_ids"] == ["legacy_1"]


def test_get_schedule_robot_offline_returns_relay_copy():
    c = FakeRelay(robot="offline")
    run(st.schedule_mission(c, "dog_t1", "coach", "07:00"))
    r = run(st.get_schedule(c, "dog_t1"))
    assert r["ok"] and r["confirmed_by_robot"] is False and len(r["missions"]) == 1
    assert "on_robot" not in r["missions"][0] and "relay's copy" in r["note"]


def test_malformed_robot_reply():
    c = FakeRelay()

    async def bad(*a, **k):
        return {"delivered": True, "result": {"ok": True, "result": {"schedules": []}}}
    c.command = bad
    assert run(st.robot_schedule(c, "dog_t1")) == {"confirmed": False, "reason": "robot_reply_malformed"}


# ---- registration + voice readback ----

def test_registration_and_scopes():
    for t in ("get_schedule", "schedule_mission", "cancel_mission"):
        assert authz.TOOL_SCOPES[t] == "missions"
    assert "draft_mission" not in authz.TOOL_SCOPES
    from app.mcp_server import tools as mt
    src = inspect.getsource(mt)
    for t in ("get_schedule", "schedule_mission", "cancel_mission"):
        assert f'@mcp.tool(name="{t}"' in src
    assert '@mcp.tool(name="draft_mission"' not in src
    from app.voice_agent import tools as vt
    names = {x["name"]: x for x in vt.TOOLS}
    assert "draft_mission" not in names
    assert {"silent_guardian", "coach"} <= set(names["schedule_mission"]["input_schema"]["properties"]["type"]["enum"])
    assert names["cancel_mission"]["input_schema"]["required"] == ["dog_id", "mission_id"]


def test_voice_readback():
    from app.voice_agent.brain import Brain
    ok = run(st.schedule_mission(FakeRelay(), "dog_t1", "silent_guardian", "18:00", None, 180))
    ch = {"tool": "schedule_mission", "result": ok}
    assert Brain._readback_sentence(ch) == "Silent Guardian at 18:00 for 180 minutes is scheduled, and the robot has confirmed it."
    assert any(t in "Done, Silent Guardian starts at 6:00 tonight." for t in Brain._readback_tokens(ch))
    unconfirmed = run(st.schedule_mission(FakeRelay(robot="timeout"), "dog_t1", "coach", "07:30"))
    assert "hasn't confirmed" in Brain._readback_sentence({"tool": "schedule_mission", "result": unconfirmed})
    c = FakeRelay()
    mid = run(st.schedule_mission(c, "dog_t1", "coach", "07:30"))["mission_id"]
    cancelled = run(st.cancel_mission(c, "dog_t1", mid))
    assert Brain._readback_sentence({"tool": "cancel_mission", "result": cancelled}) == "I've cancelled Coach mode at 07:30."


def test_real_robot_reply_shape():
    """The reply wimz_robot_05 actually sent on 2026-11-07, plus a scheduler-off variant."""
    c = FakeRelay()

    async def real(*a, **k):
        return {"delivered": True, "result": {"ok": True, "result": {
            "missions": [], "count": 0, "active": None,
            "scheduler": {"enabled": True, "now": "2026-11-07T12:10:48.783283-05:00", "check_interval_s": 5.0}}}}
    c.command = real
    r = run(st.get_schedule(c, "dog_t1"))
    assert r["confirmed_by_robot"] and r["scheduler_enabled"] is True and r["missions"] == []
    assert r["robot_time"].startswith("2026-11-07T12:10") and "warning" not in r

    async def off(*a, **k):
        return {"delivered": True, "result": {"ok": True, "result": {
            "missions": [], "scheduler": {"enabled": False}, "active": {"id": "m_x", "type": "coach"}}}}
    c.command = off
    r = run(st.get_schedule(c, "dog_t1"))
    assert r["scheduler_enabled"] is False and "will not fire" in r["warning"]
    assert r["active"] == {"id": "m_x", "type": "coach"}
