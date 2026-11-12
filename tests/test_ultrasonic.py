"""ultrasonic robot command (2026-11-12): validated before it reaches the robot; relay actor never."""
import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.services import robot_control

APP = Actor(kind="app", id="user_t1", user_id="user_t1")


@pytest.mark.parametrize("data,sent", [
    ({"action": "status"}, {"action": "status"}),
    ({"style": "before_quiet"}, {"style": "before_quiet"}),
    ({"style": "nonstop_barking"}, {"style": "nonstop_barking"}),
    ({"freq_hz": 25000, "level_pct": 40, "duration_s": 1}, {"freq_hz": 25000, "level_pct": 40, "duration_s": 1}),
    ({"freq_hz": 20000, "alt_freq_hz": 25000, "level_pct": 80, "duration_s": 5, "pulse_s": 0.5, "gap_s": 0.25},
     {"freq_hz": 20000, "alt_freq_hz": 25000, "level_pct": 80, "duration_s": 5, "pulse_s": 0.5, "gap_s": 0.25}),
])
def test_requests_pass_through(temp_db, data, sent):
    msg, _ = robot_control.build_robot_message("ultrasonic", data, APP, "dog_t1")
    assert msg["command"] == "ultrasonic" and msg["command_id"]
    assert msg["data"] == {**sent, "dog_id": "dog_t1", "source": "app"}


@pytest.mark.parametrize("data,field", [
    ({"action": "blast"}, "action"),
    ({"style": "loud"}, "style"),
    ({}, "freq_hz"),
    ({"freq_hz": 18000, "level_pct": 40, "duration_s": 1}, "freq_hz"),
    ({"freq_hz": 25000, "alt_freq_hz": 30000, "level_pct": 40, "duration_s": 1}, "alt_freq_hz"),
    ({"freq_hz": 25000, "level_pct": 81, "duration_s": 1}, "level_pct"),
    ({"freq_hz": 25000, "level_pct": True, "duration_s": 1}, "level_pct"),
    ({"freq_hz": 25000, "level_pct": 40, "duration_s": 6}, "duration_s"),
    ({"freq_hz": 25000, "level_pct": 40, "duration_s": 1, "gap_s": 9}, "gap_s"),
])
def test_bad_requests_rejected(temp_db, data, field):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("ultrasonic", data, APP, "dog_t1")
    assert e.value.status_code == 422 and e.value.detail["code"] == "bad_request" and e.value.detail["field"] == field


@pytest.mark.parametrize("kind", ["relay"])
def test_relay_cannot_fire_it(temp_db, kind):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("ultrasonic", {"action": "status"}, Actor(kind=kind, id="x", user_id="user_t1"),
                                          "dog_t1")
    assert e.value.status_code == 403 and e.value.detail["code"] == "command_not_allowed"


@pytest.mark.parametrize("kind", ["connector", "agent"])
def test_assistants_may_fire_it(temp_db, kind):
    msg, _ = robot_control.build_robot_message("ultrasonic", {"style": "before_quiet"},
                                               Actor(kind=kind, id="x", user_id="user_t1"), "dog_t1")
    assert msg["data"]["style"] == "before_quiet" and msg["data"]["source"] == kind


def test_ultrasonic_scope_is_opt_in_and_tools_wired():
    from app.mcp_server import authz
    from app.voice_agent.tools import PHYSICAL_TOOLS, TOOLS
    assert authz.TOOL_SCOPES["play_ultrasonic"] == "ultrasonic"
    assert "ultrasonic" in authz.ALL_SCOPES and "ultrasonic" in authz.OPT_IN_SCOPES and authz.SCOPE_LABELS["ultrasonic"]
    assert "play_ultrasonic" in PHYSICAL_TOOLS
    t = next(t for t in TOOLS if t["name"] == "play_ultrasonic")
    assert "owner asks" in t["description"] and not t.get("strict")


def test_ultrasonic_result_passes_robot_reply():
    out = robot_control.ultrasonic_tool_result({"command_id": "c", "result": {"ok": False, "error": {
        "code": "cooldown", "retry_after_s": 12}}})
    assert out == {"ok": False, "command_id": "c", "error": {"code": "cooldown", "retry_after_s": 12}}
    ok = robot_control.ultrasonic_tool_result({"command_id": "c", "result": {"ok": True, "result": {"style": "before_quiet"}}})
    assert ok["ok"] is True and ok["result"] == {"style": "before_quiet"}
