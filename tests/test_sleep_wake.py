"""Robot sleep / wake (2026-11-19): commands, wake-first, status, connector tool, wake greeting."""
import asyncio
import inspect

import pytest

import app.database as db
from app.auth import Actor
from app.connection_manager import get_connection_manager
from app.mcp_server import authz
from app.services import command_results, notify_service, robot_control, sleep_wake
from app.voice_agent import events as agent_events

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")


@pytest.fixture()
def rig(temp_db, monkeypatch):
    """A live robot_t that answers wake (ready) after `wake_delay` s, or never if None."""
    sleep_wake.reset()
    db.create_device_pairing("user_t1", "robot_t")
    manager = get_connection_manager()
    manager.robot_connections["robot_t"] = object()
    manager.device_owners["robot_t"] = "user_t1"
    st = {"sent": [], "hooks": [], "wake_delay": 0.05, "was_asleep": None}

    async def fake_send(device_id, message):
        st["sent"].append(dict(message))
        if message.get("command") == "wake" and st["wake_delay"] is not None:
            async def answer():
                await asyncio.sleep(st["wake_delay"])
                result = {"ready": True, "mode": "idle"}
                if st["was_asleep"] is not None:
                    result["was_asleep"] = st["was_asleep"]
                payload = {"command_id": message["command_id"], "ok": True, "result": result}
                meta = command_results.resolve(message["command_id"], payload) or {}
                sleep_wake.on_command_result(device_id, meta.get("command") or "wake", payload)
            asyncio.get_running_loop().create_task(answer())
        return True

    monkeypatch.setattr(manager, "send_to_robot", fake_send)
    monkeypatch.setattr(notify_service, "notify_agent",
                        lambda event, device_id, user_id, dog_id, payload=None, event_id=None:
                        st["hooks"].append((event, device_id, user_id)))
    yield st
    manager.robot_connections.pop("robot_t", None)
    manager.device_owners.pop("robot_t", None)
    sleep_wake.reset()


def _cmds(st):
    return [m.get("command") for m in st["sent"]]


# ---- messages ----

@pytest.mark.parametrize("data", [{"mode": "sleep"}, {"mode": "Sleeping"}, {"mode": "asleep"}])
def test_set_mode_sleep_becomes_sleep_command(temp_db, data):
    msg, _ = robot_control.build_robot_message("set_mode", data, APP, "dog_t1")
    assert msg["command"] == "sleep" and "mode" not in msg["data"]


def test_sleep_and_wake_allowed_for_app_and_assistants(temp_db):
    for actor in (APP, CONN):
        for cmd in ("sleep", "wake"):
            msg, _ = robot_control.build_robot_message(cmd, {}, actor, "dog_t1")
            assert msg["command"] == cmd


def test_app_ws_translate():
    m = sleep_wake.translate({"command": "set_mode", "data": {"mode": "sleep", "source": "app"}})
    assert m == {"command": "sleep", "data": {"source": "app"}}
    m = sleep_wake.translate({"command": "set_mode", "data": {"mode": "idle"}})
    assert m["command"] == "set_mode"


# ---- state ----

def test_robot_reported_sleep_clears_on_any_non_sleep_report():
    sleep_wake.reset()
    sleep_wake.on_mode_report("r1", "asleep", explicit=False)
    assert sleep_wake.is_asleep("r1")
    sleep_wake.on_mode_report("r1", "idle", explicit=False)
    assert not sleep_wake.is_asleep("r1")


def test_relay_sleep_ignores_stale_routine_idle():
    sleep_wake.reset()
    sleep_wake.note_sent("r1", {"command": "sleep"})
    sleep_wake.on_mode_report("r1", "idle", explicit=False)   # stale routine status
    assert sleep_wake.is_asleep("r1")
    sleep_wake.on_mode_report("r1", "idle", explicit=True)    # explicit mode_change
    assert not sleep_wake.is_asleep("r1")


def test_refused_sleep_clears_and_reconnect_downgrades():
    sleep_wake.reset()
    sleep_wake.note_sent("r1", {"command": "sleep"})
    sleep_wake.on_command_result("r1", "sleep", {"ok": False, "error": "mission_active"})
    assert not sleep_wake.is_asleep("r1")
    sleep_wake.note_sent("r1", {"command": "sleep"})
    sleep_wake.on_robot_connected("r1")                       # reboot or blip: next report decides
    sleep_wake.on_mode_report("r1", "idle", explicit=False)
    assert not sleep_wake.is_asleep("r1")


# ---- wake-first over HTTP ----

def test_action_to_sleeping_robot_wakes_first_and_greets(rig):
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    res = asyncio.run(robot_control.dispatch("dog_t1", "take_photo", {}, APP))
    assert _cmds(rig) == ["wake", "take_photo"]
    assert res["wake"]["ready"] is True and res["wake"]["woke"] is True
    assert not sleep_wake.is_asleep("robot_t")
    assert rig["hooks"] == [("robot_woke", "robot_t", "user_t1")]


@pytest.mark.parametrize("cmd,data", [("set_mode", {"mode": "silent_guardian"}), ("set_mode", {"mode": "coach"}),
                                      ("start_mission", {"mission_type": "sit"})])
def test_mode_and_mission_start_wake_first(rig, cmd, data):
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    asyncio.run(robot_control.dispatch("dog_t1", cmd, data, APP))
    assert _cmds(rig) == ["wake", cmd]


def test_awake_robot_gets_no_wake(rig):
    asyncio.run(robot_control.dispatch("dog_t1", "take_photo", {}, APP))
    assert _cmds(rig) == ["take_photo"] and rig["hooks"] == []


def test_non_action_command_does_not_wake(rig):
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    asyncio.run(robot_control.dispatch("dog_t1", "schedule_sync", {}, APP))
    assert _cmds(rig) == ["schedule_sync"] and sleep_wake.is_asleep("robot_t")


def test_wake_timeout_sends_anyway_without_greeting(rig, monkeypatch):
    monkeypatch.setattr(sleep_wake, "WAKE_READY_TIMEOUT_S", 0.1)
    rig["wake_delay"] = None
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    res = asyncio.run(robot_control.dispatch("dog_t1", "move", {"action": "forward"}, APP))
    assert _cmds(rig) == ["wake", "move"] and res["wake"]["timed_out"] is True
    assert not sleep_wake.is_asleep("robot_t") and rig["hooks"] == []


def test_explicit_wake_and_sleep_dispatch(rig):
    res = asyncio.run(robot_control.dispatch("dog_t1", "sleep", {}, APP))
    assert res["command_sent"] == "sleep" and sleep_wake.is_asleep("robot_t")
    res = asyncio.run(robot_control.dispatch("dog_t1", "wake", {}, APP))
    assert res["wake"]["ready"] and res["result"]["ok"] is True
    assert _cmds(rig) == ["sleep", "wake"] and not sleep_wake.is_asleep("robot_t")
    assert [h[0] for h in rig["hooks"]] == ["robot_woke"]


def test_wake_of_awake_robot_does_not_greet_unless_robot_says_it_slept(rig):
    asyncio.run(robot_control.dispatch("dog_t1", "wake", {}, APP))
    assert rig["hooks"] == []
    rig["was_asleep"] = True    # the relay missed the sleep; the robot knows better
    asyncio.run(robot_control.dispatch("dog_t1", "wake", {}, APP))
    assert [h[0] for h in rig["hooks"]] == ["robot_woke"]


def test_concurrent_actions_share_one_wake(rig):
    sleep_wake.note_sent("robot_t", {"command": "sleep"})

    async def main():
        await asyncio.gather(robot_control.dispatch("dog_t1", "take_photo", {}, APP),
                             robot_control.dispatch("dog_t1", "look", {"action": "center"}, APP))
    asyncio.run(main())
    assert _cmds(rig).count("wake") == 1 and _cmds(rig)[0] == "wake"
    assert sorted(_cmds(rig)[1:]) == ["look", "take_photo"]


# ---- wake-first over the app WebSocket ----

def test_app_ws_action_held_until_awake(rig):
    manager = get_connection_manager()

    async def main():
        assert await manager.forward_command_to_robot("user_t1", "robot_t",
                                                      {"command": "set_mode", "data": {"mode": "sleep"}})
        assert sleep_wake.is_asleep("robot_t")
        ok = await manager.forward_command_to_robot("user_t1", "robot_t",
                                                    {"command": "set_mode", "data": {"mode": "idle"},
                                                     "timestamp": 1})
        await asyncio.sleep(0.01)
        assert ok and _cmds(rig) == ["sleep", "wake"]          # held, not sent yet
        await asyncio.sleep(0.2)
    asyncio.run(main())
    assert _cmds(rig) == ["sleep", "wake", "set_mode"]
    assert rig["sent"][-1]["timestamp"] > 1                    # re-stamped for the robot's stale check
    assert not sleep_wake.is_asleep("robot_t")


def test_app_ws_drive_frames_start_wake_and_drop(rig):
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    manager = get_connection_manager()

    async def main():
        for _ in range(3):
            await manager.forward_command_to_robot("user_t1", "robot_t", {"command": "motor", "data": {}})
        await asyncio.sleep(0.2)
    asyncio.run(main())
    assert _cmds(rig) == ["wake"]


# ---- status, connector tool, greeting ----

def test_status_reports_sleep_over_stale_idle(rig):
    from fastapi.testclient import TestClient
    from app.auth import create_access_token
    from app.main import app
    from app.services import event_store
    asyncio.run(event_store.ingest_events(
        [{"event_id": "st1", "type": "status", "ts": "2026-11-19T10:00:00+00:00",
          "data": {"battery": 90, "mode": "idle"}}], device_id="robot_t", user_id="user_t1", source="robot"))
    sleep_wake.note_sent("robot_t", {"command": "sleep"})
    token = create_access_token({"sub": "user_t1"})
    r = TestClient(app).get("/api/dogs/dog_t1/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "sleep" and body["asleep"] is True
    assert body.get("live_mode", "sleep") == "sleep"   # live_mode comes with the replay-mode work


def test_robot_status_event_spelling_normalised(rig):
    from app.services import event_store
    ev, _ = event_store.validate_event({"event_id": "s", "type": "status", "ts": "2026-11-19T10:00:00+00:00",
                                        "data": {"battery": 80, "mode": "Sleeping"}})
    assert ev["data"]["mode"] == "sleep"
    asyncio.run(event_store.ingest_events(
        [{"event_id": "mc1", "type": "mode_change", "ts": "2026-11-19T10:00:00+00:00", "data": {"mode": "asleep"}}],
        device_id="robot_t", user_id="user_t1", source="robot"))
    assert sleep_wake.is_asleep("robot_t")


def test_connector_tool_registered_under_settings_scope():
    from app.mcp_server import tools as mt
    assert authz.TOOL_SCOPES["set_sleep"] == "settings"
    assert '@mcp.tool(name="set_sleep"' in inspect.getsource(mt)


def test_sleep_tool_result_shapes():
    ok = robot_control.sleep_tool_result(True, {"delivered": True, "result": {"ok": True}})
    assert ok["confirmed"] and ok["message"] == "WIM-Z is asleep."
    refused = robot_control.sleep_tool_result(True, {"delivered": True,
                                                     "result": {"ok": False, "error": "mission_active"}})
    assert refused["ok"] is False and refused["error"] == "mission_active"
    woke = robot_control.sleep_tool_result(False, {"delivered": True, "wake": {"ready": True, "woke": True}})
    assert woke["ready"] and "awake" in woke["message"]


def test_wake_greeting_shares_boot_debounce():
    agent_events._last_spoken.clear()
    assert "robot_woke" in agent_events.GREETING_EVENTS and "robot_woke" in agent_events.DEBOUNCE_S
    assert not agent_events._debounced("robot_connected", "robot_t")
    # handle() maps robot_woke onto the robot_connected bucket, so a wake right after boot is debounced
    assert agent_events._debounced("robot_connected", "robot_t")
    agent_events._last_spoken.clear()
