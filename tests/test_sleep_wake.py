"""Robot sleep / wake (2026-11-19; ROBOT-STATE v2): set_sleep (C3), sleep is not a mode, wake-first,
status, connector tool, wake greeting. Uses the FakeRobot in tests/conftest.py."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.connection_manager import get_connection_manager
from app.mcp_server import authz
from app.services import robot_control, robot_state, sleep_wake
from app.voice_agent import events as agent_events
from tests.conftest import BEZIK

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")
DEV = "robot_t"


def run(c):
    return asyncio.run(c)


def asleep_now(robot):
    robot.asleep = True
    robot_state.update(DEV, source="robot", asleep=True)


# ---- messages ----

def test_set_sleep_for_app_and_assistants(temp_db):
    for actor in (APP, CONN):
        for value in (True, False):
            msg, _ = robot_control.build_robot_message("set_sleep", {"asleep": value, "reason": "x"}, actor, "dog_t1")
            assert msg["command"] == "set_sleep"
            assert msg["data"] == {"asleep": value, "source": actor.kind, "reason": "x"}


def test_legacy_sleep_wake_and_sleep_mode_are_gone(temp_db):
    for cmd in ("sleep", "wake"):
        with pytest.raises(HTTPException) as e:
            robot_control.build_robot_message(cmd, {}, APP, "dog_t1")
        assert e.value.status_code == 403
    for mode in ("sleep", "Sleeping", "asleep"):
        with pytest.raises(HTTPException) as e:
            robot_control.build_robot_message("set_mode", {"mode": mode}, APP, "dog_t1")
        assert e.value.detail["code"] == "invalid_mode"
    with pytest.raises(HTTPException):
        robot_control.build_robot_message("set_sleep", {"asleep": "yes"}, APP, "dog_t1")


# ---- wake-first ----

def test_action_to_sleeping_robot_wakes_first_and_greets(robot):
    async def main():
        robot.connect()
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "take_photo", {}, APP)
    run(main())
    assert robot.commands() == ["set_sleep", "take_photo"]
    assert robot.sent[0]["data"] == {"asleep": False, "source": "app", "reason": "take_photo"}
    assert robot_state.get(DEV)["asleep"] is False
    assert [h[0] for h in robot.hooks] == ["robot_woke"]


@pytest.mark.parametrize("cmd,data", [("set_mode", {"mode": "coach"}), ("start_mission", {"mission_type": "sit"})])
def test_mode_and_mission_start_wake_first(robot, cmd, data):
    async def main():
        robot.connect()
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, cmd, data, APP)
    run(main())
    assert robot.commands()[:2] == ["set_sleep", cmd]


def test_awake_robot_gets_no_wake_and_every_command_wakes_a_sleeping_one(robot):
    async def main():
        robot.connect()
        await robot_control.dispatch(BEZIK, "look", {"action": "center"}, APP)
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "schedule_sync", {}, APP)      # not an "action": wakes too
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": True}, APP)   # set_sleep never wakes first
    run(main())
    assert robot.commands() == ["look", "set_sleep", "schedule_sync", "set_sleep"]
    assert robot.sent[-1]["data"]["asleep"] is True


def test_wake_timeout_fails_the_action_without_retry_or_greeting(robot):
    robot.respond = False

    async def main():
        robot.connect()
        asleep_now(robot)
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "take_photo", {}, APP)
        assert e.value.status_code == 504 and e.value.detail["code"] == "robot_timeout"
    run(main())
    assert robot.commands() == ["set_sleep"]           # the photo was dropped, not queued
    assert robot_state.get(DEV)["asleep"] is True and robot.hooks == []


def test_explicit_sleep_and_wake_dispatch(robot):
    async def main():
        robot.connect()
        r1 = await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": True}, CONN)
        assert robot_state.get(DEV)["asleep"] is True
        r2 = await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False}, CONN)
        return r1, r2
    r1, r2 = run(main())
    assert robot_control.sleep_tool_result(True, r1)["message"] == "WIM-Z is asleep."
    assert robot_control.sleep_tool_result(False, r2)["message"] == "WIM-Z is awake and ready."
    assert robot_state.get(DEV)["asleep"] is False and robot_state.get(DEV)["mode"] == "idle"
    assert [h[0] for h in robot.hooks] == ["robot_woke"]


def test_concurrent_actions_share_one_wake(robot):
    async def main():
        robot.connect()
        asleep_now(robot)
        await asyncio.gather(robot_control.dispatch(BEZIK, "take_photo", {}, APP),
                             robot_control.dispatch(BEZIK, "look", {"action": "center"}, APP))
    run(main())
    assert robot.commands().count("set_sleep") == 1 and robot.commands()[0] == "set_sleep"
    assert sorted(robot.commands()[1:]) == ["look", "take_photo"]


# ---- wake-first over the app WebSocket ----

def test_app_ws_action_held_until_awake(robot):
    manager = get_connection_manager()

    async def main():
        robot.connect()
        asleep_now(robot)
        ok = await manager.forward_command_to_robot("user_t1", DEV, {"command": "look", "data": {"action": "center"},
                                                                     "timestamp": 1})
        await asyncio.sleep(0.01)
        assert ok and robot.commands() == ["set_sleep"]          # held, not sent yet
        await asyncio.sleep(0.2)
    run(main())
    assert robot.commands() == ["set_sleep", "look"]
    assert robot.sent[-1]["timestamp"] > 1                       # re-stamped for the robot's stale check
    assert robot_state.get(DEV)["asleep"] is False


def test_app_ws_drive_frames_start_wake_and_drop(robot):
    manager = get_connection_manager()

    async def main():
        robot.connect()
        asleep_now(robot)
        for _ in range(3):
            await manager.forward_command_to_robot("user_t1", DEV, {"command": "motor", "data": {}})
        await asyncio.sleep(0.2)
    run(main())
    assert robot.commands() == ["set_sleep"]


# ---- status, connector tool, greeting ----

def test_status_reports_asleep_with_mode_idle(robot):
    from fastapi.testclient import TestClient
    from app.auth import create_access_token
    from app.main import app
    robot_state.update(DEV, source="robot", asleep=True)
    token = create_access_token({"sub": "user_t1"})
    r = TestClient(app).get(f"/api/dogs/{BEZIK}/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "idle" and body["asleep"] is True


def test_legacy_sleep_spelling_in_status_becomes_asleep(robot):
    from app.services import event_store
    ev, _ = event_store.validate_event({"event_id": "s", "type": "status", "ts": "2026-11-19T10:00:00+00:00",
                                        "data": {"battery": 80, "mode": "Sleeping"}})
    assert ev["data"]["mode"] == "idle" and ev["data"]["asleep"] is True
    run(event_store.ingest_events([{"event_id": "s2", "type": "status", "ts": "2026-11-19T10:00:00+00:00",
                                    "data": {"battery": 80, "mode": "sleep"}}],
                                  device_id=DEV, user_id="user_t1", source="robot", notify=False, forward=False))
    assert robot_state.get(DEV)["asleep"] is True and robot_state.get(DEV)["mode"] == "idle"


def test_connector_tool_registered_under_settings_scope():
    from app.mcp_server import tools as mt
    assert authz.TOOL_SCOPES["set_sleep"] == "settings"
    src = inspect.getsource(mt)
    assert '@mcp.tool(name="set_sleep"' in src and 'client.command(dog_id, "set_sleep"' in src


def test_sleep_tool_result_shapes():
    ok = robot_control.sleep_tool_result(True, {"delivered": True, "result": {
        "success": True, "result": {"asleep": True, "mode": "idle"}}})
    assert ok["ok"] and ok["asleep"] is True and ok["mode"] == "idle" and ok["message"] == "WIM-Z is asleep."
    refused = robot_control.sleep_tool_result(True, {"delivered": True,
                                                     "result": {"success": False, "error": "mission_active"}})
    assert refused["ok"] is False and refused["error"] == "mission_active"


def test_wake_greeting_shares_boot_debounce():
    agent_events._last_spoken.clear()
    assert "robot_woke" in agent_events.GREETING_EVENTS and "robot_woke" in agent_events.DEBOUNCE_S
    assert not agent_events._debounced("robot_connected", "robot_t")
    # handle() maps robot_woke onto the robot_connected bucket, so a wake right after boot is debounced
    assert agent_events._debounced("robot_connected", "robot_t")
    agent_events._last_spoken.clear()
