"""smart_plug robot command and event (robot 248f9e5, 2026-11-11)."""
import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.event_buffer import FEED_WORTHY_EVENTS
from app.mcp_server import authz
from app.routers import websocket as ws
from app.services import robot_control
from app.voice_agent.tools import TOOLS

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")


@pytest.mark.parametrize("action", sorted(robot_control.SMART_PLUG_ACTIONS - {"configure"}))
def test_app_actions_pass_through(temp_db, action):
    msg, _ = robot_control.build_robot_message("smart_plug", {"action": action}, APP, "dog_t1")
    assert msg["type"] == "command" and msg["command"] == "smart_plug" and msg["command_id"]
    assert msg["data"] == {"action": action, "source": "app"}


def test_configure_keeps_only_given_fields(temp_db):
    msg, _ = robot_control.build_robot_message(
        "smart_plug", {"action": "configure", "device_id": "8006AB", "auto_off_minutes": 90}, APP, "dog_t1")
    assert msg["data"] == {"action": "configure", "device_id": "8006AB", "auto_off_minutes": 90, "source": "app"}
    msg, _ = robot_control.build_robot_message(
        "smart_plug", {"action": "configure", "device_id": "8006AB", "alias": "Diffuser", "trigger_level": 3},
        APP, "dog_t1")
    assert msg["data"]["alias"] == "Diffuser" and msg["data"]["trigger_level"] == 3


@pytest.mark.parametrize("data,code", [
    ({"action": "explode"}, "invalid_action"),
    ({}, "invalid_action"),
    ({"action": "configure"}, "missing_device_id"),
    ({"action": "configure", "device_id": "x", "trigger_level": 5}, "invalid_value"),
    ({"action": "configure", "device_id": "x", "auto_off_minutes": 0}, "invalid_value"),
    ({"action": "configure", "device_id": "x", "auto_off_minutes": True}, "invalid_value"),
    ({"action": "configure", "device_id": "x", "alias": 7}, "invalid_value"),
])
def test_bad_requests_rejected(temp_db, data, code):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("smart_plug", data, APP, "dog_t1")
    assert e.value.status_code == 422 and e.value.detail["code"] == code


@pytest.mark.parametrize("actor", [CONN, AGENT])
@pytest.mark.parametrize("action", ["scan", "configure", "remove", "toggle"])
def test_assistants_only_on_off_status(temp_db, actor, action):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("smart_plug", {"action": action, "device_id": "x"}, actor, "dog_t1")
    assert e.value.status_code == 403 and e.value.detail["code"] == "smart_plug_action_not_allowed"
    msg, _ = robot_control.build_robot_message("smart_plug", {"action": "on"}, actor, "dog_t1")
    assert msg["data"]["action"] == "on"


def test_tool_result():
    ok = robot_control.smart_plug_tool_result("on", {"command_id": "c", "result": {"ok": True, "result": {
        "configured": True, "alias": "Diffuser", "device_id": "8006AB", "on": True, "reachable": True,
        "owner": "manual", "auto_off_in_s": None, "trigger_level": 2, "auto_off_minutes": 120}}})
    assert ok == {"ok": True, "action": "on", "command_id": "c", "configured": True, "alias": "Diffuser",
                  "on": True, "reachable": True, "owner": "manual", "trigger_level": 2, "auto_off_minutes": 120}
    gone = robot_control.smart_plug_tool_result("status", {"command_id": "c", "result": {
        "ok": False, "error": {"code": "plug_unreachable", "message": "timeout"}}})
    assert gone["ok"] is False and gone["error"] == "plug_unreachable" and gone["available"] is False
    none = robot_control.smart_plug_tool_result("status", {"result": {"ok": True, "result": {"configured": False}}})
    assert none["configured"] is False and "No smart plug" in none["message"]
    assert robot_control.smart_plug_tool_result("off", {"timed_out": True})["reason"] == "smart_plug_timed_out"


def test_tool_wiring():
    assert authz.TOOL_SCOPES["set_smart_plug"] == "settings"
    t = next(t for t in TOOLS if t["name"] == "set_smart_plug")
    assert not t.get("strict")
    assert t["input_schema"]["properties"]["action"]["enum"] == ["off", "on", "status"]


def test_event_routed_like_guardian():
    assert "smart_plug" in FEED_WORTHY_EVENTS
    assert ws.LEGACY_TO_ACTIVITY_TYPE["smart_plug"] == "smart_plug"
    msg = {"type": "smart_plug", "on": True, "reason": "sg_level", "level": 2}
    ws._normalize_robot_event(msg)
    assert msg["event"] == "smart_plug"
    legacy = {"type": "event", "event": "guardian"}
    ws._normalize_robot_event(legacy)
    assert legacy["event"] == "guardian"


def test_event_persisted_flat(monkeypatch):
    stored = {}

    def fake_insert(**kw):
        stored.update(kw)
        return {"inserted": False}

    monkeypatch.setattr(ws, "db_insert_activity_event", fake_insert)
    ws._maybe_persist_as_activity("dev1", "user_t1", {
        "type": "smart_plug", "event": "smart_plug", "on": False, "reason": "timer", "alias": "Diffuser",
        "owner": "robot", "auto_off_at": None, "device_id": "dev1", "timestamp": "2026-11-11T10:00:00Z"})
    assert stored["type"] == "smart_plug"
    p = stored["payload"]
    assert p["on"] is False and p["reason"] == "timer" and p["alias"] == "Diffuser" and p["event"] == "smart_plug"
    assert "device_id" not in p and "type" not in p
