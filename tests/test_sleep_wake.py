"""Robot sleep / wake (2026-11-19; ROBOT-STATE v2): set_sleep (C3), sleep is not a mode, wake-first,
status, connector tool, wake greeting. Uses the FakeRobot in tests/conftest.py."""
import asyncio
import inspect
import time

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.connection_manager import get_connection_manager
from app.mcp_server import authz
from app.services import robot_control, robot_state, sleep_wake
from app.voice_agent import events as agent_events
from tests.conftest import BEZIK, ELSA

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")
DEV = "robot_t"


def run(c):
    return asyncio.run(c)


def woke_hooks(robot):
    return [h for h in robot.hooks if h[0] == "robot_woke"]


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

def test_action_to_sleeping_robot_wakes_first_without_greeting(robot):
    async def main():
        robot.connect()
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "take_photo", {}, APP)
    run(main())
    assert robot.commands() == ["set_sleep", "take_photo"]
    assert robot.sent[0]["data"] == {"asleep": False, "source": "app", "reason": "take_photo"}
    assert robot_state.get(DEV)["asleep"] is False
    assert woke_hooks(robot) == []          # the relay's own wake-first never greets


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
    assert robot_state.get(DEV)["asleep"] is True and woke_hooks(robot) == []


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
    assert len(woke_hooks(robot)) == 1


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


# ---- wake greeting (2026-11-20): explicit wake only, within 1000 ms, not twice ----

def test_1_explicit_wake_from_app_and_connector_greets_after_success(robot):
    """1. set_sleep {asleep: false} from the app (WebSocket) or a connector that returns success."""
    from tests.test_robot_state import FakeWS, app_command

    async def main():
        robot.connect()
        asleep_now(robot)
        await app_command(FakeWS(), {"type": "command", "command": "set_sleep",
                                     "data": {"asleep": False, "reason": "wake_button"}})
        await asyncio.sleep(0.2)
        assert len(woke_hooks(robot)) == 1 and woke_hooks(robot)[0][3]["cause"] == "wake_button"
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False}, CONN)
        assert len(woke_hooks(robot)) == 2
    run(main())
    assert robot.commands() == ["set_sleep", "set_sleep"]


def test_1_failed_or_unanswered_wake_does_not_greet(robot):
    robot.respond = False

    async def main():
        robot.connect()
        asleep_now(robot)
        with pytest.raises(HTTPException):
            await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False}, CONN)
    run(main())
    assert woke_hooks(robot) == []


def test_1_going_to_sleep_does_not_greet(robot):
    async def main():
        robot.connect()
        await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": True}, CONN)
    run(main())
    assert woke_hooks(robot) == [] and [h[0] for h in robot.hooks] == ["greeting_prepare"]


@pytest.mark.parametrize("how", ["wake_first_dispatch", "wake_first_app_ws", "select_dog", "download_song",
                                 "robot_status", "agent_wake"])
def test_2_no_greeting_for_wake_first_select_dog_download_song(robot, how):
    """2. Never for the relay's wake-first before another command, nor select_dog / download_song,
    nor a robot that reports itself awake, nor a wake that is not from the app or a connector."""
    manager = get_connection_manager()

    async def main():
        robot.connect()
        asleep_now(robot)
        if how == "wake_first_dispatch":
            await robot_control.dispatch(BEZIK, "take_photo", {}, CONN)
        elif how == "wake_first_app_ws":
            await manager.forward_command_to_robot("user_t1", DEV, {"type": "command", "command": "look",
                                                                   "data": {"action": "center"}})
            await asyncio.sleep(0.2)
        elif how == "select_dog":
            await robot_state.select_dog(DEV, BEZIK, "app")
            await asyncio.sleep(0.1)
        elif how == "download_song":
            await sleep_wake.send_awake(DEV, {"type": "command", "command": "download_song", "command_id": "d1",
                                              "data": {"source": "app"}}, "app")
        elif how == "robot_status":
            robot_state.update(DEV, source="robot", reason="status", asleep=False)
        else:
            await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False},
                                         Actor(kind="agent", id="voice", user_id="user_t1"))
    run(main())
    if how.startswith("wake_first"):
        assert robot.commands()[0] == "set_sleep" and robot_state.get(DEV)["asleep"] is False
    assert woke_hooks(robot) == []


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    """The voice agent with a prepared-clip TTS, a fixed roll-up and a recording play_tts."""
    seen = {"pushed": [], "synth": 0, "summary": []}

    class Client:
        def __init__(self, actor, user_id):
            pass

        async def get_status(self, dog_id):
            return {"timezone": "UTC", "quiet_hours": {"active": False}}

        async def get_summary(self, dog_id, day):
            seen["summary"].append((dog_id, day))
            return {"date": "2026-11-19", "counts": {"bark_episode": 1}, "totals": {}}

    class Brain:
        async def speak_event(self, intent, data):
            seen["intent"] = intent
            return None   # the fallback line

    async def synth(text, cache_key=None):
        seen["synth"] += 1
        return f"clip{seen['synth']}", f"https://x/clip{seen['synth']}.mp3"

    async def push_clip(user_id, dog_id, clip_id, url, device_id=None):
        seen["pushed"].append({"at": time.time(), "dog_id": dog_id, "clip_id": clip_id, "device_id": device_id})
        return {"delivered": True}

    monkeypatch.setattr(agent_events, "RelayClient", Client)
    monkeypatch.setattr(agent_events, "get_brain", lambda: Brain())
    monkeypatch.setattr(agent_events.tts, "synthesize", synth)
    monkeypatch.setattr(agent_events.tts, "clip_path", lambda cid: cid and tmp_path / "x.mp3")
    (tmp_path / "x.mp3").write_bytes(b"")
    monkeypatch.setattr(agent_events, "push_clip", push_clip)
    monkeypatch.setattr(agent_events, "say", lambda user_id, dog_id, text, device_id=None, cache_key=None:
                        push_clip(user_id, dog_id, "boot", "u", device_id=device_id))
    monkeypatch.setattr(agent_events.get_settings(), "agent_boot_greeting_delay_seconds", 0)
    for d in (agent_events._last_spoken, agent_events._greeting_sent, agent_events._prepared,
              agent_events._seen_ids):
        d.clear()
    yield seen
    for d in (agent_events._last_spoken, agent_events._greeting_sent, agent_events._prepared):
        d.clear()


def _woke(n=0):
    return {"event": "robot_woke", "event_id": f"w{n}-{time.time()}", "device_id": DEV, "user_id": "user_t1",
            "payload": {"cause": "wake_button", "result_at": time.time()}}


def test_3_prepared_greeting_goes_out_within_1000_ms(robot, agent):
    """3. Recorded while asleep, so play_tts leaves within 1000 ms of the wake's command_result."""
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    run(agent_events.handle({"event": "greeting_prepare", "event_id": "p1", "device_id": DEV,
                             "user_id": "user_t1", "payload": {"cause": "asleep"}}))
    hook = _woke()
    out = run(agent_events.handle(hook))
    assert out["send_ms"] < 1000 and agent["pushed"][0]["at"] - hook["payload"]["result_at"] < 1.0
    assert agent["synth"] == 1                               # nothing recorded after the wake
    assert agent["pushed"][0]["dog_id"] == BEZIK and "Bezik" in out["text"]


def test_4_selection_change_while_asleep_is_greeted_by_the_new_dog(robot, agent):
    """4. selected_dog_id names the dog and picks the recap; no dog selected: neither."""
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    run(agent_events.prepare_greeting("user_t1", DEV))
    robot_state.update(DEV, source="app", selected_dog_id=ELSA)
    out = run(agent_events.handle(_woke(1)))
    assert out["dog_id"] == ELSA and "Elsa" in out["text"] and "Bezik" not in out["text"]
    assert agent["summary"][-1] == (ELSA, "yesterday")
    robot_state.update(DEV, source="app", selected_dog_id=None)
    agent["summary"].clear()
    out = run(agent_events.handle(_woke(2)))
    assert out["dog_id"] is None and agent["summary"] == []
    assert "Elsa" not in out["text"] and "Bezik" not in out["text"] and "Rex" not in out["text"]


def test_5_no_wake_greeting_within_60_s_of_a_power_on_greeting(robot, agent):
    """5. A power-on greeting sent < 60 s ago -> the wake greeting is skipped; older -> it plays."""
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    boot = run(agent_events.handle({"event": "robot_connected", "event_id": "b1", "device_id": DEV,
                                    "user_id": "user_t1"}))
    assert boot["delivered"] and len(agent["pushed"]) == 1
    assert run(agent_events.handle(_woke(3))) == {"skipped": "boot_greeting_recent"}
    assert len(agent["pushed"]) == 1
    agent_events._greeting_sent[(DEV, "boot")] -= 61
    assert run(agent_events.handle(_woke(4)))["delivered"] and len(agent["pushed"]) == 2


def test_5_power_on_greeting_skipped_right_after_a_wake_greeting(robot, agent):
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    assert run(agent_events.handle(_woke(5)))["delivered"]
    out = run(agent_events.handle({"event": "robot_connected", "event_id": "b2", "device_id": DEV,
                                   "user_id": "user_t1"}))
    assert out == {"skipped": "wake_greeting_recent"} and len(agent["pushed"]) == 1


def test_5_wake_greetings_are_not_debounced_against_each_other(robot, agent):
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    assert run(agent_events.handle(_woke(6)))["delivered"]
    assert run(agent_events.handle(_woke(7)))["delivered"]
    assert agent["synth"] == 1 and len(agent["pushed"]) == 2   # one recording, played twice


def test_play_sound_right_after_a_wake_gets_6_s(robot):
    """The robot took 3.5 s to answer the wake greeting's play_sound (2026-11-20): within 10 s of a
    wake, play_sound gets the 6000 ms budget; otherwise the usual 2000 ms."""
    agent = Actor(kind="agent", id="voice", user_id="user_t1")

    async def main():
        robot.connect()
        asleep_now(robot)
        await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False}, CONN)
        robot.delay = 3.0
        out = await robot_control.dispatch(BEZIK, "play_tts", {"url": "https://x/c.mp3", "clip_id": "c"}, agent)
        assert out["result"]["success"] is True
        robot_state._woke_at.clear()
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "play_tts", {"url": "https://x/c.mp3", "clip_id": "c"}, agent)
        assert e.value.detail["code"] == "robot_timeout"
    run(main())


@pytest.mark.parametrize("frame", [
    {"type": "command", "command": "sg_config", "data": {"sensitivity": "medium"}},
    {"type": "command", "command": "reload_dogs"},
    {"type": "command", "command": "smart_plug", "data": {"action": "status"}},
])
def test_app_reopen_frames_reach_a_sleeping_robot_without_waking_it(robot, frame):
    """2026-11-20: reopening the app re-sends sg_config, reload_dogs and a smart_plug status check;
    they go to the sleeping robot as they are and it stays asleep."""
    manager = get_connection_manager()

    async def main():
        robot.connect()
        asleep_now(robot)
        assert await manager.forward_command_to_robot("user_t1", DEV, dict(frame))
        await asyncio.sleep(0.2)
    run(main())
    assert robot.commands() == [frame["command"]] and robot_state.get(DEV)["asleep"] is True
    assert woke_hooks(robot) == []


def test_smart_plug_switch_still_wakes_a_sleeping_robot(robot):
    manager = get_connection_manager()

    async def main():
        robot.connect()
        asleep_now(robot)
        await manager.forward_command_to_robot("user_t1", DEV, {"type": "command", "command": "smart_plug",
                                                                "data": {"action": "on"}})
        await asyncio.sleep(0.2)
    run(main())
    assert robot.commands() == ["set_sleep", "smart_plug"]
