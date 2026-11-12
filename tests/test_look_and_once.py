"""look camera step and one-time missions (robot d7f235b, 2026-11-12)."""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.mcp_server import authz
from app.services import robot_control, schedule_tools, settings_service
from app.voice_agent.tools import PHYSICAL_TOOLS, TOOLS

CONN = Actor(kind="connector", id="claude", user_id="user_t1")


@pytest.mark.parametrize("data,sent", [
    ({"action": "pan_left"}, {"action": "pan_left", "degrees": 15}),
    ({"action": "tilt_up", "degrees": 45}, {"action": "tilt_up", "degrees": 45}),
    ({"action": "center", "degrees": 99}, {"action": "center"}),
])
def test_look_command(temp_db, data, sent):
    msg, _ = robot_control.build_robot_message("look", data, CONN, "dog_t1")
    assert msg["command"] == "look" and msg["data"] == {**sent, "source": "connector"}


@pytest.mark.parametrize("data,code", [
    ({"action": "spin"}, "invalid_action"),
    ({"action": "pan_right", "degrees": 4}, "invalid_value"),
    ({"action": "pan_right", "degrees": 46}, "invalid_value"),
    ({"action": "pan_right", "degrees": True}, "invalid_value"),
])
def test_look_rejected(temp_db, data, code):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("look", data, CONN, "dog_t1")
    assert e.value.status_code == 422 and e.value.detail["code"] == code


def test_look_wiring_and_result():
    assert authz.TOOL_SCOPES["look"] == "physical" and "look" in PHYSICAL_TOOLS
    assert any(t["name"] == "look" for t in TOOLS)
    out = robot_control.look_tool_result("pan_left", {"command_id": "c", "result": {"ok": True, "result": {
        "pan": -15, "tilt": 0, "limit_reached": False, "moved_degrees": 15}}})
    assert out == {"ok": True, "action": "pan_left", "command_id": "c", "pan": -15, "tilt": 0,
                   "limit_reached": False, "moved_degrees": 15}
    busy = robot_control.look_tool_result("tilt_up", {"result": {"ok": False, "error": {"code": "gimbal_busy"}}})
    assert busy["ok"] is False and busy["error"] == "gimbal_busy"


def test_once_date_resolution(temp_db):
    tz = settings_service.get_timezone("dog_t1")
    now = datetime.now(tz)
    later = (now + timedelta(hours=2)).strftime("%H:%M")
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    assert schedule_tools.resolve_once_date("dog_t1", None, "10:00") == (None, None)
    assert schedule_tools.resolve_once_date("dog_t1", "", "10:00") == (None, None)
    assert schedule_tools.resolve_once_date("dog_t1", "tomorrow", "10:00") == (tomorrow, None)
    assert schedule_tools.resolve_once_date("dog_t1", tomorrow, "10:00") == (tomorrow, None)
    if now.hour < 22:
        assert schedule_tools.resolve_once_date("dog_t1", "today", later)[0] == now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    assert schedule_tools.resolve_once_date("dog_t1", yesterday, "10:00")[1]["reason"] == "date_in_past"
    assert schedule_tools.resolve_once_date("dog_t1", "next week", "10:00")[1]["reason"] == "date_must_be_YYYY-MM-DD"
    assert schedule_tools.resolve_once_date("dog_t1", "2026-02-30", "10:00")[1]["reason"] == "date_must_be_YYYY-MM-DD"
    far = (now.date() + timedelta(days=400)).isoformat()
    assert schedule_tools.resolve_once_date("dog_t1", far, "10:00")[1]["reason"] == "date_too_far_ahead"


def test_missions_setting_keeps_date_and_omits_it_when_unset():
    v = settings_service.validate_value("missions", {"missions": [
        {"id": "m_a", "type": "recall", "trigger": {"kind": "schedule", "time": "10:00", "days": ["sat"],
                                                    "date": "2026-11-14"}},
        {"id": "m_b", "type": "recall", "trigger": {"kind": "schedule", "time": "11:00", "days": ["mon"]}}]})
    assert v["missions"][0]["trigger"]["date"] == "2026-11-14"
    assert "date" not in v["missions"][1]["trigger"] and "grace_s" not in v["missions"][1]["trigger"]
    with pytest.raises(HTTPException):
        settings_service.validate_value("missions", {"missions": [
            {"id": "m_c", "type": "recall", "trigger": {"kind": "schedule", "time": "10:00", "date": "14/11/2026"}}]})
