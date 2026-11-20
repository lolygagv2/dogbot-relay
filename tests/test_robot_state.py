"""ROBOT-STATE v2 (2026-11-19): relay-owned robot state, select_dog, C1/C2/C3, no queueing,
E1 mode_change, E2 robot_state frames, H1/H2, status, greeting. T1..T11 of the contract, run
against a fake robot (tests/conftest.py FakeRobot) that answers like the contract says."""
import asyncio
import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.routers import event_store as status_routes
from app.routers import robot_state as state_routes
from app.routers import websocket as ws
from app.services import command_results, robot_control, robot_state
from tests.conftest import BEZIK, ELSA

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")
DEV = "robot_t"
ROOT = Path(__file__).resolve().parent.parent


def run(c):
    return asyncio.run(c)


async def until(cond, timeout=5.0):
    """Wait for cond() to be true; returns the seconds it took (raises after timeout)."""
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
    return time.monotonic() - t0


class FakeWS:
    def __init__(self):
        self.frames = []

    async def send_json(self, msg):
        self.frames.append((time.monotonic(), msg))


async def app_command(sock, message):
    """One app frame through the relay's /ws/app command path (preflight + forward)."""
    from app.connection_manager import get_connection_manager
    handled, on_sent, on_fail = await ws._app_command_preflight(sock, "user_t1", DEV, message)
    if not handled:
        await get_connection_manager().forward_command_to_robot("user_t1", DEV, message, on_sent=on_sent,
                                                                on_fail=on_fail)
    return handled


def st():
    return robot_state.get(DEV)


# ---------------------------------------------------------------- state basics


def test_defaults_and_e2_shape(robot):
    s = robot_state.public(DEV)
    assert list(s) == ["type", "device_id", "robot_online", "mode", "asleep", "selected_dog_id",
                       "selected_dog_applied", "session_dog_id", "state_version", "updated_at", "source", "reason"]
    assert s["type"] == "robot_state" and s["mode"] == "idle" and s["robot_online"] is False


def test_version_bumps_and_updated_at_format(robot):
    v0 = st()["state_version"]
    robot_state.update(DEV, source="app", reason="dropdown", mode="coach")
    s = st()
    assert s["state_version"] == v0 + 1 and s["source"] == "app" and s["reason"] == "dropdown"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", s["updated_at"])
    robot_state.update(DEV, source="app", mode="coach")          # no change -> no bump
    assert st()["state_version"] == v0 + 1


def test_rules_r4_r5(robot):
    robot_state.update(DEV, source="app", mode="silent_guardian", session_dog_id=ELSA)
    robot_state.update(DEV, source="app", asleep=True)                       # R5
    assert (st()["mode"], st()["session_dog_id"], st()["asleep"]) == ("idle", None, True)
    robot_state.update(DEV, source="app", asleep=False)
    assert st()["mode"] == "idle"
    robot_state.update(DEV, source="app", mode="coach", session_dog_id=ELSA)
    robot_state.update(DEV, source="app", mode="manual")                     # R4
    assert st()["session_dog_id"] is None


def test_r6_other_internal_states_read_idle(robot):
    for m in ("night_sentry", "photography", "emergency", "shutdown", "docking", "sleep", "guardian"):
        assert robot_state.normalize_mode(m) == "idle"
        assert robot_state.valid_mode(m) is None


def test_state_survives_a_relay_restart(robot):
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    robot_state._first_status_pending.clear()        # in-memory parts gone; the row stays
    assert st()["selected_dog_id"] == BEZIK


def test_first_status_after_connect_sets_mode_then_status_is_ignored(robot):
    robot_state.update(DEV, source="app", mode="silent_guardian", session_dog_id=ELSA)

    async def main():
        robot.connect()
        robot_state.on_status(DEV, {"mode": "idle", "battery": 80})         # rebooted robot
        assert st()["mode"] == "idle"
        robot_state.on_status(DEV, {"mode": "coach", "battery": 80})        # later heartbeat: ignored
        assert st()["mode"] == "idle"
        robot_state.on_status(DEV, {"mode": "idle", "asleep": True})        # asleep always applies
        assert st()["asleep"] is True
    run(main())


# ---------------------------------------------------------------- T1 / T2 select_dog


def test_t1_select_online(robot):
    async def main():
        robot.connect()
        t0 = time.monotonic()
        body = await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        answered = time.monotonic() - t0
        assert body["selected_dog_id"] == BEZIK and body["type"] == "robot_state"
        applied = await until(lambda: st()["selected_dog_applied"], 2.0)
        return answered, applied
    answered, applied = run(main())
    assert answered <= 1.0 and applied <= 2.0
    c2 = [m for m in robot.sent if m["command"] == "select_dog"][-1]
    assert c2["data"] == {"dog_id": BEZIK, "source": "app"} and c2["command_id"]
    assert robot.selected == BEZIK
    assert robot.states()[-1]["selected_dog_applied"] is True
    print(f"T1 answered_ms={answered * 1000:.0f} applied_ms={applied * 1000:.0f}")


def test_t2_select_offline_then_connect(robot):
    async def main():
        body = await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP)
        assert (body["selected_dog_id"], body["selected_dog_applied"], body["robot_online"]) == (ELSA, False, False)
        assert robot.commands() == []                       # nothing sent, nothing queued but the value
        robot.connect()
        return await until(lambda: st()["selected_dog_applied"], 5.0)
    took = run(main())
    assert took <= 5.0 and robot.commands().count("select_dog") == 1
    print(f"T2 applied_after_connect_ms={took * 1000:.0f}")


def test_select_timeout_stays_unapplied_and_resends_once_on_next_connect(robot):
    robot.respond = False

    async def main():
        robot.connect()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP)
        await asyncio.sleep(2.2)
        assert st()["selected_dog_applied"] is False and robot.commands().count("select_dog") == 1
        await asyncio.sleep(0.5)
        assert robot.commands().count("select_dog") == 1    # no retry while connected
        robot.respond = True
        robot.disconnect()
        robot.connect()     # no selection report from this robot: the stored one goes after 3 s
        await until(lambda: st()["selected_dog_applied"], 5.0)
    run(main())
    assert robot.commands().count("select_dog") == 2


def test_last_write_wins_and_unknown_dog(robot):
    async def main():
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP)
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        assert st()["selected_dog_id"] == BEZIK
        robot.connect()     # reports nothing: the stored selection goes after the 3 s fallback
        await until(lambda: st()["selected_dog_applied"], 5.0)
        with pytest.raises(HTTPException) as e:
            await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id="nope"), actor=APP)
        assert e.value.status_code == 422
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=None), actor=APP)
        assert st()["selected_dog_id"] is None
    run(main())
    assert [m["data"]["dog_id"] for m in robot.sent if m["command"] == "select_dog"][:1] == [BEZIK]


def test_h1_h2_over_http(robot):
    from fastapi.testclient import TestClient
    from app.auth import create_access_token
    from app.main import app
    h = {"Authorization": f"Bearer {create_access_token({'sub': 'user_t1'})}"}
    c = TestClient(app)
    r = c.post(f"/api/devices/{DEV}/selected_dog", json={"dog_id": BEZIK}, headers=h)
    assert r.status_code == 200 and r.json()["selected_dog_id"] == BEZIK
    g = c.get(f"/api/devices/{DEV}/state", headers=h)
    assert g.status_code == 200 and g.json() == r.json()
    other = {"Authorization": f"Bearer {create_access_token({'sub': 'user_t2'})}"}
    assert c.get(f"/api/devices/{DEV}/state", headers=other).status_code == 404


def test_select_changes_only_via_select_dog_r2(robot):
    async def main():
        robot.connect()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        await until(lambda: st()["selected_dog_applied"])
        await robot_control.dispatch(ELSA, "set_mode", {"mode": "coach", "dog_id": ELSA}, CONN)
        await robot_control.dispatch(ELSA, "set_mode", {"mode": "idle"}, CONN)
    run(main())
    assert st()["selected_dog_id"] == BEZIK


# ---------------------------------------------------------------- T3 / T4 offline, nothing queued


def test_t3_app_set_mode_offline_then_reconnect(robot):
    sock = FakeWS()

    async def main():
        v0 = st()["state_version"]
        t0 = time.monotonic()
        await app_command(sock, {"type": "command", "command": "set_mode", "command_id": "c-t3",
                                 "data": {"mode": "silent_guardian", "reason": "dropdown"}})
        took = sock.frames[0][0] - t0
        assert st()["state_version"] == v0
        robot.connect()
        await asyncio.sleep(1.0)      # stands in for the contract's 60 s: nothing queued means nothing sent
        return took
    took = run(main())
    frame = sock.frames[0][1]
    assert frame["type"] == "command_result" and frame["success"] is False and frame["error"] == "robot_offline"
    assert frame["command_id"] == "c-t3" and took <= 0.5
    assert "set_mode" not in robot.commands() and st()["mode"] == "idle"
    print(f"T3 offline_reply_ms={took * 1000:.0f}")


def test_t3_http_offline_and_no_retry(robot):
    with pytest.raises(HTTPException) as e:
        run(robot_control.dispatch(BEZIK, "set_mode", {"mode": "silent_guardian"}, APP))
    assert e.value.detail["code"] == "robot_offline"


def test_t4_connector_offline(robot, monkeypatch):
    """The MCP tool path: the relay's 409 robot_offline -> {ok: false, refused: true, reason}."""
    from app.services.relay_client import RelayRefusal
    try:
        run(robot_control.dispatch(BEZIK, "set_mode", robot_control.sg_mode_command(True, BEZIK), CONN))
    except HTTPException as e:
        out = RelayRefusal(e.status_code, e.detail["code"], e.detail).as_result()
    assert out["ok"] is False and out["refused"] is True and out["reason"] == "robot_offline"
    import inspect
    from app.mcp_server import tools as mt
    src = inspect.getsource(mt)
    assert src.count("Nothing is queued") >= 3   # set_silent_guardian, set_coach, set_sleep descriptions


def test_timeout_is_robot_timeout_and_state_unchanged(robot):
    robot.respond = False

    async def main():
        robot.connect()
        v0 = st()["state_version"]
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "set_mode", {"mode": "coach"}, CONN)
        assert e.value.detail["code"] == "robot_timeout" and 1.9 <= time.monotonic() - t0 <= 2.6
        assert st()["state_version"] == v0 and st()["mode"] == "idle"
        sock = FakeWS()
        await app_command(sock, {"type": "command", "command": "set_mode", "command_id": "c-to",
                                 "data": {"mode": "coach", "reason": "dropdown"}})
        await asyncio.sleep(2.3)
        return sock
    sock = run(main())
    assert robot.commands().count("set_mode") == 2          # one each, never retried
    assert sock.frames[-1][1]["error"] == "robot_timeout" and sock.frames[-1][1]["success"] is False


def test_invalid_mode(robot):
    sock = FakeWS()

    async def main():
        robot.connect()
        for bad in ("sleep", "Silent_Guardian", "night_sentry", None):
            await app_command(sock, {"type": "command", "command": "set_mode", "data": {"mode": bad}})
    run(main())
    assert all(f["error"] == "invalid_mode" and f["success"] is False for _, f in sock.frames)
    assert "set_mode" not in robot.commands()
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("set_mode", {"mode": "sleep"}, APP, BEZIK)
    assert e.value.detail["code"] == "invalid_mode"


def test_app_frame_source_is_set_by_relay(robot):
    sock = FakeWS()

    async def main():
        robot.connect()
        await app_command(sock, {"type": "command", "command": "set_mode", "source": "connector",
                                 "data": {"mode": "coach", "source": "agent", "reason": "dropdown", "timestamp": 123}})
        await until(lambda: st()["mode"] == "coach")
    run(main())
    c1 = [m for m in robot.sent if m["command"] == "set_mode"][-1]
    assert c1["data"] == {"mode": "coach", "dog_id": None, "source": "app", "reason": "dropdown", "timestamp": 123}
    assert "source" not in c1 and st()["source"] == "app" and st()["reason"] == "dropdown"


# ---------------------------------------------------------------- T5 / T6 / T7 sessions


def test_t5_connector_sg_for_another_dog(robot):
    async def main():
        robot.connect()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        await until(lambda: st()["selected_dog_applied"])
        n = len(robot.states())
        t0 = time.monotonic()
        res = await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(True, ELSA), CONN)
        await until(lambda: any(m.get("mode") == "silent_guardian" for m in robot.states()[n:]), 2.0)
        return res, time.monotonic() - t0
    res, took = run(main())
    tool = robot_control.sg_mode_tool_result(True, res)
    assert tool["ok"] and tool["session_dog_id"] == ELSA
    s = st()
    assert (s["mode"], s["session_dog_id"], s["selected_dog_id"], s["source"]) == ("silent_guardian", ELSA, BEZIK, "connector")
    assert took <= 2.0
    from app.database import list_device_events
    mc = list_device_events(DEV, ["mode_change"])[-1]
    assert mc["dog_id"] is None and mc["data"]["session_dog_id"] == ELSA and mc["data"]["source"] == "connector"
    assert set(mc["data"]) >= {"mode", "previous_mode", "session_dog_id", "source", "reason", "command_id",
                               "previous_duration_s"}
    print(f"T5 app_shows_sg_ms={took * 1000:.0f} state={s['mode']}/{s['session_dog_id']}/{s['selected_dog_id']}")


def test_t5_relay_keeps_the_robots_attribution(robot):
    """The treat's dog_id / id_method come from the robot (section 3); the relay stores them as sent."""
    from app.database import list_events
    from app.services import event_store
    run(event_store.ingest_events([{"event_id": "tr1", "type": "treat", "dog_id": ELSA,
                                    "ts": "2026-11-19T12:00:00Z", "data": {"source": "auto", "id_method": "session"}}],
                                  device_id=DEV, user_id="user_t1", source="robot", notify=False, forward=False))
    row = [r for r in list_events(ELSA, types=["treat"]) if r["event_id"] == "tr1"][0]
    assert row["dog_id"] == ELSA and row["data"]["id_method"] == "session"


def test_t6_sg_off(robot):
    async def main():
        robot.connect()
        await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(True, ELSA), CONN)
        t0 = time.monotonic()
        await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(False, ELSA), CONN)
        await until(lambda: robot.states()[-1]["mode"] == "idle", 2.0)
        return time.monotonic() - t0
    took = run(main())
    assert st()["mode"] == "idle" and st()["session_dog_id"] is None and took <= 2.0
    print(f"T6 app_shows_idle_ms={took * 1000:.0f}")


def test_t7_no_dog_selected_mode_still_starts(robot):
    sock = FakeWS()

    async def main():
        robot.connect()
        assert st()["selected_dog_id"] is None
        await app_command(sock, {"type": "command", "command": "set_mode", "data": {"mode": "silent_guardian"}})
        await until(lambda: st()["mode"] == "silent_guardian", 2.0)
    run(main())
    assert st()["session_dog_id"] is None and not sock.frames


def test_session_falls_back_to_selected_dog_r3(robot):
    async def main():
        robot.connect()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        await until(lambda: st()["selected_dog_applied"])
        await robot_control.dispatch(BEZIK, "set_mode", {"mode": "coach"}, APP)
    run(main())
    assert st()["session_dog_id"] == BEZIK


def test_mode_change_from_the_robot_itself_updates_state(robot):
    async def main():
        robot.connect()
        robot.mode, robot.session = "mission", ELSA
        await robot.post_mode_change("idle", "schedule", "mission_start", None)
        await until(lambda: st()["mode"] == "mission")
    run(main())
    assert st()["session_dog_id"] == ELSA and st()["source"] == "schedule"


def test_e2_on_app_connect(robot):
    sock = FakeWS()
    robot_state.update(DEV, source="app", selected_dog_id=BEZIK)
    run(robot_state.send_all_to_app(sock.send_json, [DEV]))
    assert sock.frames[0][1] == robot_state.public(DEV)


# ---------------------------------------------------------------- T8 greeting


@pytest.mark.parametrize("dog,name", [(BEZIK, "Bezik"), (ELSA, "Elsa"), (None, None)])
def test_t8_wake_greeting_names_the_selected_dog(robot, monkeypatch, dog, name):
    from app.voice_agent import events as agent_events
    seen = {"summary": [], "intent": None, "pushed": None}

    class Client:
        def __init__(self, actor, user_id):
            pass

        async def get_summary(self, dog_id, day):
            seen["summary"].append((dog_id, day))
            return {"date": "2026-11-18", "counts": {"bark_episode": 2}, "totals": {}}

    class Brain:
        async def speak_event(self, intent, data):
            seen["intent"] = intent
            return None   # use the fallback line

    async def synth(text, cache_key=None):
        return "clip", "https://x/clip.mp3"

    async def push_clip(user_id, dog_id, clip_id, url, device_id=None):
        seen["pushed"] = dog_id
        return {"delivered": True}

    monkeypatch.setattr(agent_events, "RelayClient", Client)
    monkeypatch.setattr(agent_events, "get_brain", lambda: Brain())
    monkeypatch.setattr(agent_events.tts, "synthesize", synth)
    monkeypatch.setattr(agent_events.tts, "clip_path", lambda cid: cid)
    monkeypatch.setattr(agent_events, "push_clip", push_clip)
    for d in (agent_events._greeting_sent, agent_events._prepared):
        d.clear()
    robot_state.update(DEV, source="app", selected_dog_id=dog)

    async def wake():
        robot.connect()
        _asleep(robot)
        await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": False}, APP)   # a real wake
    run(wake())
    assert robot.hooks and robot.hooks[-1][0] == "robot_woke"
    out = run(agent_events.handle({"event": "robot_woke", "event_id": f"w-{dog}", "device_id": DEV,
                                   "user_id": "user_t1", "payload": robot.hooks[-1][3]}))
    said = out["text"]
    assert seen["pushed"] == dog
    if name:
        assert seen["summary"] == [(dog, "yesterday")] and name in seen["intent"] and name in said
        other = "Elsa" if name == "Bezik" else "Bezik"
        assert other not in seen["intent"] and other not in said
    else:
        assert seen["summary"] == [] and "Elsa" not in said and "Bezik" not in said
        assert "Rex" not in said
    for d in (agent_events._greeting_sent, agent_events._prepared):
        d.clear()


# ---------------------------------------------------------------- T9 replay, T10 sleep


def test_t9_replay_status(robot, monkeypatch):
    from app.services import replayer as rp

    class R:
        device_id, date = "robot_demo", "2026-11-18"

        def handles(self, dog_id):
            return dog_id == BEZIK

        def virtual_time(self):
            from datetime import datetime, timezone
            return datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)

        def virtual_mode(self):
            return "silent_guardian"

    monkeypatch.setattr(rp, "get_replayer", lambda: R())
    robot_state.update(DEV, source="robot", mode="coach")
    s = run(status_routes.get_dog_status(BEZIK, actor=APP))
    assert s["mode"] == "coach" and s["replay"]["active"] is True
    assert s["replay"] == {"active": True, "virtual_mode": "silent_guardian",
                           "virtual_time": "2026-11-18T10:00:00+00:00", "date": "2026-11-18"}
    for gone in ("replay_active", "demo_replay_active", "virtual_time", "replay_date", "live_mode", "mode_source"):
        assert gone not in s
    print(f"T9 status.mode={s['mode']} replay={s['replay']}")


def test_t10_set_sleep(robot):
    async def main():
        robot.connect()
        await robot_control.dispatch(BEZIK, "set_mode", {"mode": "coach"}, APP)
        res = await robot_control.dispatch(BEZIK, "set_sleep", {"asleep": True}, CONN)
        return res
    res = run(main())
    assert robot_control.sleep_tool_result(True, res)["asleep"] is True
    assert st()["asleep"] is True and st()["mode"] == "idle"
    c3 = [m for m in robot.sent if m["command"] == "set_sleep"][-1]
    assert c3["data"] == {"asleep": True, "source": "connector", "reason": None}
    s = run(status_routes.get_dog_status(BEZIK, actor=APP))
    assert s["mode"] == "idle" and s["asleep"] is True
    modes = {m["mode"] for m in robot.states()}
    assert "sleep" not in modes and modes <= set(robot_state.MODES)


# ---------------------------------------------------------------- T11 grep proof


def test_t11_grep_proof():
    def grep(pattern):
        out = subprocess.run(["grep", "-rnE", "--include=*.py", pattern, "app"], cwd=ROOT,
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines() if ".bak" not in ln]
    assert grep(r"last_dog") == []
    assert grep(r"id_method[\"']?\s*[:=]\s*[\"']vision") == []
    assert grep(r"mode_changed") == []


# ---------------------------------------------------------------- round 2: source -> reason, select_dog tool,
# schedule_mission dog_id, dog-less speech, no oldest-dog lookup


def test_relay_stamps_source_and_moves_caller_source_to_reason(robot):
    msg, _ = robot_control.build_robot_message("set_mode", {"mode": "coach", "source": "dropdown"}, APP, BEZIK)
    assert msg["data"]["source"] == "app" and msg["data"]["reason"] == "dropdown"
    msg, _ = robot_control.build_robot_message("set_mode", {"mode": "coach", "source": "x", "reason": "y"}, CONN, BEZIK)
    assert msg["data"]["source"] == "connector" and msg["data"]["reason"] == "y"
    # every command carries the relay's source, including ones whose payload had none
    msg, _ = robot_control.build_robot_message("start_mission", {"mission_type": "sit", "source": "agent"}, APP, BEZIK)
    assert msg["data"]["source"] == "app" and msg["data"]["reason"] == "agent"


def test_app_frames_are_stamped(robot):
    sock = FakeWS()

    async def main():
        robot.connect()
        await app_command(sock, {"type": "command", "command": "set_mode", "source": "drive_enter",
                                 "data": {"mode": "manual"}})
        await app_command(sock, {"type": "command", "command": "look", "source": "joystick",
                                 "data": {"action": "center", "source": "app_v45"}})
        await app_command(sock, {"type": "command", "command": "motor", "source": "stick", "left": 0.1})
        await until(lambda: st()["mode"] == "manual")
    run(main())
    by = {m["command"]: m for m in robot.sent}
    assert by["set_mode"]["data"]["source"] == "app" and by["set_mode"]["data"]["reason"] == "drive_enter"
    assert by["look"]["data"] == {"action": "center", "source": "app", "reason": "app_v45"} and "source" not in by["look"]
    assert by["motor"]["source"] == "app" and by["motor"]["reason"] == "stick" and by["motor"]["left"] == 0.1
    assert st()["reason"] == "drive_enter"


def test_select_dog_connector_tool(robot):
    """The MCP / voice select_dog tool: H2 on each of the owner's robots, settings scope."""
    from app.mcp_server import authz

    class Client:   # RelayClient stand-in calling the real H2 route
        async def select_dog(self, device_id, dog_id):
            return await state_routes.post_selected_dog(device_id, state_routes.SelectedDogRequest(dog_id=dog_id),
                                                        actor=CONN)

    async def main():
        off = await robot_control.select_dog_for_owner(Client(), ELSA)
        robot.connect()
        await until(lambda: st()["selected_dog_applied"])
        on = await robot_control.select_dog_for_owner(Client(), BEZIK)
        await until(lambda: st()["selected_dog_applied"])
        return off, on
    off, on = run(main())
    assert off["ok"] and off["robots"][0]["robot_online"] is False and off["robots"][0]["selected_dog_applied"] is False
    assert "reconnects" in off["message"]
    assert on["ok"] and on["selected_dog_id"] == BEZIK and st()["selected_dog_id"] == BEZIK and st()["source"] == "connector"
    c2 = [m for m in robot.sent if m["command"] == "select_dog"][-1]
    assert c2["data"] == {"dog_id": BEZIK, "source": "connector"}
    assert authz.TOOL_SCOPES["select_dog"] == "settings"
    import inspect
    from app.mcp_server import tools as mt
    from app.voice_agent import tools as vt
    assert '@mcp.tool(name="select_dog"' in inspect.getsource(mt)
    assert "select_dog" in {t["name"] for t in vt.TOOLS}


def test_schedule_mission_carries_the_session_dog(robot):
    from app.models import Missions
    from app.services import schedule_tools

    class Client:
        def __init__(self):
            self.missions = []

        async def get_setting(self, dog_id, key):
            return {"value": {"missions": self.missions}, "version": 1}

        async def put_setting(self, dog_id, key, value, expected_version=None):
            self.missions = Missions.model_validate(value).model_dump()["missions"]
            return {"value": value, "version": 2}

        async def command(self, dog_id, command, data, wait_s=None, device_id=None):
            raise robot_control.HTTPException(status_code=409)

    c = Client()
    from app.services.relay_client import RelayRefusal

    async def refuse(*a, **k):
        raise RelayRefusal(409, "robot_offline", {"code": "robot_offline"})
    c.command = refuse
    out = run(schedule_tools.schedule_mission(c, ELSA, "silent_guardian", "18:00"))
    assert out["ok"] and c.missions[0]["data"] == {"dog_id": ELSA}


def test_device_only_command_path(robot):
    AGENT = Actor(kind="agent", id="agent", user_id="user_t1")

    async def main():
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch_device(DEV, "play_tts", {"url": "https://x/a.mp3"}, AGENT)
        assert e.value.detail["code"] == "robot_offline"
        robot.connect()
        res = await robot_control.dispatch_device(DEV, "play_tts", {"url": "https://x/a.mp3"}, AGENT)
        assert res["delivered"] and robot.commands()[-1] == "play_sound"
        for cmd, actor in (("dispense_treat", AGENT), ("play_tts", APP), ("play_tts", CONN)):
            with pytest.raises(HTTPException) as e:
                await robot_control.dispatch_device(DEV, cmd, {}, actor)
            assert e.value.status_code == 403
    run(main())


def test_no_dog_greeting_speaks_through_the_robot_and_never_borrows_a_dog(robot, monkeypatch):
    from app.voice_agent import events as agent_events
    calls = {}

    class Client:
        def __init__(self, actor, user_id):
            pass

        async def get_status(self, dog_id):
            calls["status"] = dog_id
            return {}

        async def get_summary(self, dog_id, day):
            calls["summary"] = dog_id
            return {}

        async def device_command(self, device_id, command, data):
            calls["device_command"] = (device_id, command)
            return {"delivered": True}

        async def command(self, *a, **k):
            calls["command"] = a
            return {"delivered": True}

    class Brain:
        async def speak_event(self, intent, data):
            return None

    async def synth(text, cache_key=None):
        return "clip", "https://x/clip.mp3"

    monkeypatch.setattr(agent_events, "RelayClient", Client)
    monkeypatch.setattr(agent_events, "get_brain", lambda: Brain())
    monkeypatch.setattr(agent_events.tts, "synthesize", synth)
    monkeypatch.setattr(agent_events.get_settings(), "agent_boot_greeting_delay_seconds", 0)
    agent_events._last_spoken.clear()
    out = run(agent_events.handle({"event": "robot_connected", "event_id": "nodog", "device_id": DEV,
                                   "user_id": "user_t1"}))
    assert out["dog_id"] is None and out["text"].endswith("I'm back on watch.")
    assert calls == {"device_command": (DEV, "play_tts")}          # no status / summary / command for any dog
    agent_events._last_spoken.clear()


def test_robot_utterance_with_no_dog_selected_runs_with_no_dog(robot, monkeypatch):
    from app.voice_agent import app as agent_app
    seen = {}

    class Brain:
        async def respond(self, **kw):
            seen.update(kw)
            return {"text_out": "Hello.", "tools_used": [], "refused": []}

    monkeypatch.setattr(agent_app, "get_brain", lambda: Brain())
    caller = agent_app.Caller("user_t1", "robot", DEV)
    body, _ = run(agent_app._run_text(caller, None, "how was today?", want_tts=False))
    assert body["text_out"] == "Hello." and seen["dog_hint"] is None
    assert seen["extra_data"]["selected_dog"] is None and seen["ctx"].role == "owner"


# ---------------------------------------------------------------- round 3: migration (adopt the robot's
# selected dog) and set_mode to a sleeping robot


def _robot_status(eid, **data):
    from app.services import event_store
    run(event_store.ingest_events([{"event_id": eid, "type": "status", "ts": "2026-11-19T12:00:00Z",
                                    "data": {"battery": 80, "mode": "idle", **data}}],
                                  device_id=DEV, user_id="user_t1", source="robot", notify=False, forward=False))


def test_migration_first_connect_sends_nothing_and_adopts_the_robots_dog(robot):
    from app.database import get_connection
    with get_connection() as conn:   # no state record for this robot yet
        assert conn.execute("SELECT COUNT(*) FROM robot_state WHERE device_id = ?", (DEV,)).fetchone()[0] == 0

    async def main():
        robot.connect()
        await asyncio.sleep(0.3)
        assert "select_dog" not in robot.commands()          # never select_dog null on first connect
        assert st()["selected_dog_id"] is None and st()["selected_dog_applied"] is False
    run(main())
    _robot_status("m1", selected_dog_id=BEZIK)               # the robot's _current_dog_id
    s = st()
    assert (s["selected_dog_id"], s["selected_dog_applied"], s["source"], s["reason"]) == \
        (BEZIK, True, "robot", "adopted_from_robot")
    assert robot_state.owns_selection(s)
    # from now on the relay owns it: a later robot report does not change it, a reconnect sends nothing
    _robot_status("m2", selected_dog_id=ELSA)
    assert st()["selected_dog_id"] == BEZIK

    async def again():
        robot.disconnect()
        robot.connect()
        await asyncio.sleep(0.3)
        assert "select_dog" not in robot.commands()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP)
        await until(lambda: st()["selected_dog_applied"])
    run(again())
    assert st()["selected_dog_id"] == ELSA and robot.commands().count("select_dog") == 1


def test_migration_over_the_websocket_status_and_reported_null(robot):
    robot_state.on_status(DEV, ws._status_fields({"type": "status_update", "selected_dog_id": None,
                                                  "data": {"mode": "idle", "battery": 70}}))
    s = st()
    assert s["selected_dog_id"] is None and s["selected_dog_applied"] is True and robot_state.owns_selection(s)


def test_migration_owner_choice_before_any_report_wins(robot):
    run(state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP))
    _robot_status("m3", selected_dog_id=BEZIK)
    assert st()["selected_dog_id"] == ELSA and st()["selected_dog_applied"] is False


def test_migration_ignores_a_dog_that_is_not_the_owners(robot):
    _robot_status("m4", selected_dog_id="someone-elses-dog")
    assert st()["selected_dog_id"] is None and not robot_state.owns_selection(st())


def test_set_mode_to_a_sleeping_robot_wakes_first(robot):
    """Relay knows the robot is asleep: set_sleep false (<= 2 s) first, then set_mode (<= 2 s)."""
    async def main():
        robot.connect()
        robot.asleep = True
        robot_state.update(DEV, source="robot", asleep=True)
        t0 = time.monotonic()
        res = await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(True, ELSA), CONN)
        return res, time.monotonic() - t0
    res, took = run(main())
    assert robot.commands() == ["set_sleep", "set_mode"] and res["result"]["success"] is True
    assert st()["mode"] == "silent_guardian" and st()["asleep"] is False and took < 4.0


def test_late_reply_after_timeout_still_updates_state(robot):
    """The robot answers after the 2 s window: the caller already got robot_timeout, and the relay
    still records what the robot then reports (its late command_result / mode_change)."""
    robot.delay = 2.5

    async def main():
        robot.connect()
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "set_mode", {"mode": "coach"}, APP)
        assert e.value.detail["code"] == "robot_timeout" and st()["mode"] == "idle"
        await until(lambda: st()["mode"] == "coach", 2.0)
    run(main())
    assert robot.commands().count("set_mode") == 1


# ---------------------------------------------------------------- round 4: wake-first for every command,
# 6000 ms wake / 2000 ms everything else, "expired"


def _asleep(robot):
    robot.asleep = True
    robot_state.update(DEV, source="robot", asleep=True)


def test_r4_mode_command_to_a_sleeping_robot_succeeds(robot):
    """Wake takes 3 s (inside the 6000 ms budget), then set_mode (2000 ms)."""
    robot.wake_delay = 3.0

    async def main():
        robot.connect()
        _asleep(robot)
        t0 = time.monotonic()
        res = await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(True, ELSA), CONN)
        return res, time.monotonic() - t0
    res, took = run(main())
    assert robot.commands() == ["set_sleep", "set_mode"]
    assert robot.sent[0]["data"] == {"asleep": False, "source": "connector", "reason": "set_mode"}
    assert res["result"]["success"] is True and res["wake"]["ready"] is True
    s = st()
    assert (s["asleep"], s["mode"], s["session_dog_id"]) == (False, "silent_guardian", ELSA)
    assert 3.0 <= took < 3.5
    print(f"R4-success total_ms={took * 1000:.0f} commands={robot.commands()} state={s['mode']}/{s['session_dog_id']}")


def test_r4_wake_not_confirmed_is_robot_timeout_and_nothing_else_is_sent(robot):
    robot.wake_delay = 7.0   # answers after the 6000 ms budget

    async def main():
        robot.connect()
        _asleep(robot)
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(ELSA, "set_mode", robot_control.sg_mode_command(True, ELSA), CONN)
        took = time.monotonic() - t0
        assert robot.commands() == ["set_sleep"]          # set_mode never sent
        await asyncio.sleep(1.3)                          # the late wake reply arrives (7 s)
        return e.value, took
    err, took = run(main())
    from app.services.relay_client import RelayRefusal
    tool = RelayRefusal(err.status_code, err.detail["code"], err.detail).as_result()
    assert tool == {"ok": False, "refused": True, "reason": "robot_timeout"}
    assert 5.9 <= took <= 6.5
    assert robot.commands() == ["set_sleep"] and st()["mode"] == "idle"
    assert st()["asleep"] is False                       # rule 7: the late wake reply is still applied
    print(f"R4-timeout after_ms={took * 1000:.0f} sent={robot.commands()} tool={tool}")


def test_r4_wake_not_confirmed_over_the_app_websocket(robot):
    robot.wake_delay = 7.0
    sock = FakeWS()

    async def main():
        robot.connect()
        _asleep(robot)
        await app_command(sock, {"type": "command", "command": "look", "command_id": "c-look",
                                 "data": {"action": "center"}})
        await asyncio.sleep(6.3)
    run(main())
    assert robot.commands() == ["set_sleep"]
    assert sock.frames[-1][1] == robot_state.command_result_frame(DEV, "look", "c-look", "robot_timeout")


def test_r4_robot_replies_expired(robot):
    robot.expire = {"set_mode", "dispense_treat"}

    async def main():
        robot.connect()
        outs = []
        for cmd, data in (("set_mode", robot_control.sg_mode_command(True, ELSA)), ("dispense_treat", {})):
            with pytest.raises(HTTPException) as e:
                await robot_control.dispatch(ELSA, cmd, data, CONN)
            outs.append(e.value)
        sock = FakeWS()
        await app_command(sock, {"type": "command", "command": "set_mode", "command_id": "c-exp",
                                 "data": {"mode": "coach"}})
        await asyncio.sleep(0.3)
        return outs, sock
    outs, sock = run(main())
    from app.services.relay_client import RelayRefusal
    for e in outs:
        assert RelayRefusal(e.status_code, e.detail["code"], e.detail).as_result() == \
            {"ok": False, "refused": True, "reason": "expired"}
    fwd = [m for _, m in robot.app_frames if m.get("type") == "command_result" and m.get("command_id") == "c-exp"][0]
    assert fwd["success"] is False and fwd["error"] == "expired"     # passed to the app unchanged
    assert st()["mode"] == "idle" and not sock.frames                  # no relay timeout on top
    print(f"R4-expired http={[e.detail for e in outs]} app_frame_error={fwd['error']}")


def test_r4_expired_wake_sends_nothing_else(robot):
    robot.expire = {"set_sleep"}

    async def main():
        robot.connect()
        _asleep(robot)
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "take_photo", {}, APP)
        return e.value
    err = run(main())
    assert err.detail == {"code": "expired"} and robot.commands() == ["set_sleep"]


def test_r4_relay_made_commands_select_dog_and_download_song_do_not_wake(robot):
    """Point 10: the robot applies select_dog and download_song while asleep; no wake, no greeting."""
    async def main():
        robot.connect()
        _asleep(robot)
        from app.services import sleep_wake
        assert await sleep_wake.send_awake(DEV, {"type": "command", "command": "download_song",
                                                 "command_id": "dl1", "data": {"source": "app"}}, "app") is None
        await robot_control.dispatch(BEZIK, "schedule_sync", {}, APP)   # any other command still wakes
    run(main())
    assert robot.commands() == ["download_song", "set_sleep", "schedule_sync"]


def test_r4_every_command_waits_2000_ms(robot):
    robot.respond = False

    async def main():
        robot.connect()
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as e:
            await robot_control.dispatch(BEZIK, "dispense_treat", {}, APP)
        return e.value, time.monotonic() - t0
    err, took = run(main())
    assert err.detail == {"code": "robot_timeout"} and 1.9 <= took <= 2.5


# ---------------------------------------------------------------- round 5: reconnect rule, select_dog asleep


def _stored_elsa_then_disconnect(robot):
    """The relay owns an applied selection of Elsa; the robot then goes offline."""
    async def main():
        robot.connect()
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP)
        await until(lambda: st()["selected_dog_applied"])
        robot.disconnect()
    run(main())
    robot.sent.clear()
    return st()["state_version"]


def _reconnect_reporting(robot, dog, flag):
    robot.selected = dog
    robot.report = {"selected_dog_id": dog, "selected_dog_changed_locally": flag}

    async def main():
        robot.connect()
        await asyncio.sleep(0.4)
    run(main())
    return [m["data"]["dog_id"] for m in robot.sent if m["command"] == "select_dog"]


def test_r5_i_local_selection_on_the_robot_is_adopted(robot):
    v0 = _stored_elsa_then_disconnect(robot)
    sent = _reconnect_reporting(robot, BEZIK, True)
    s = st()
    assert (s["selected_dog_id"], s["selected_dog_applied"], s["source"], s["reason"]) == \
        (BEZIK, True, "robot", "local_selection")
    assert sent == [BEZIK] and s["state_version"] >= v0 + 2          # connect, adopt (+1), applied
    assert any(m.get("selected_dog_id") == BEZIK and m["reason"] == "local_selection" for m in robot.states())
    print(f"R5-i state={s['selected_dog_id'] == BEZIK and 'Bezik'} applied={s['selected_dog_applied']} "
          f"select_dog_sent={['Bezik' for _ in sent]}")


def test_r5_i_local_selection_wins_over_a_stored_unapplied_one(robot):
    _stored_elsa_then_disconnect(robot)
    run(state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP))
    robot_state.update(DEV, source="app", selected_dog_applied=False)          # stored, not yet applied
    sent = _reconnect_reporting(robot, BEZIK, True)
    assert st()["selected_dog_id"] == BEZIK and st()["selected_dog_applied"] and sent == [BEZIK]


def test_r5_ii_no_local_change_the_stored_value_is_sent(robot):
    _stored_elsa_then_disconnect(robot)
    sent = _reconnect_reporting(robot, BEZIK, False)
    s = st()
    assert (s["selected_dog_id"], s["selected_dog_applied"]) == (ELSA, True) and sent == [ELSA]
    assert robot.selected == ELSA
    print(f"R5-ii state=Elsa applied={s['selected_dog_applied']} select_dog_sent={['Elsa' for _ in sent]}")


def test_r5_ii_flag_absent_behaves_as_false(robot):
    _stored_elsa_then_disconnect(robot)
    robot.selected = BEZIK
    robot.report = {"selected_dog_id": BEZIK}

    async def main():
        robot.connect()
        await asyncio.sleep(0.4)
    run(main())
    assert st()["selected_dog_id"] == ELSA and [m["data"]["dog_id"] for m in robot.sent
                                                  if m["command"] == "select_dog"] == [ELSA]


def test_r5_iii_another_accounts_dog_is_ignored(robot):
    import app.database as db
    db.create_user("user_t9", "other@example.com", "x")
    db.create_dog(dog_id="dog_other", name="Max", user_id="user_t9")
    db.add_user_dog("user_t9", "dog_other", "owner")
    v0 = _stored_elsa_then_disconnect(robot)
    sent = _reconnect_reporting(robot, "dog_other", True)
    s = st()
    assert s["selected_dog_id"] == ELSA and s["reason"] != "local_selection"
    assert sent == [ELSA] and s["selected_dog_applied"] is True     # handled as (c): stored value sent
    print(f"R5-iii state=Elsa sent={['Elsa' for _ in sent]} version {v0}->{s['state_version']}")


def test_r5_d_values_match_nothing_is_sent(robot):
    v0 = _stored_elsa_then_disconnect(robot)
    sent = _reconnect_reporting(robot, ELSA, False)
    assert sent == [] and st()["selected_dog_id"] == ELSA and st()["state_version"] == v0 + 1   # connect only


def test_r5_iv_select_dog_to_a_sleeping_robot_does_not_wake_it(robot):
    async def main():
        robot.connect()
        _asleep(robot)
        await state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=BEZIK), actor=APP)
        await until(lambda: st()["selected_dog_applied"], 2.0)
    run(main())
    s = st()
    assert "set_sleep" not in robot.commands() and robot.commands() == ["select_dog"]
    assert s["asleep"] is True and s["selected_dog_applied"] is True and s["selected_dog_id"] == BEZIK
    assert [h for h in robot.hooks if h[0] == "robot_woke"] == []
    print(f"R5-iv sent={robot.commands()} asleep={s['asleep']} applied={s['selected_dog_applied']}")


def test_r6_d_matching_report_confirms_an_unapplied_selection(robot):
    """Point 13: owner picks Elsa while the robot is offline; it reconnects already reporting Elsa
    (flag false) -> applied true and a robot_state frame, nothing sent to the robot."""
    run(state_routes.post_selected_dog(DEV, state_routes.SelectedDogRequest(dog_id=ELSA), actor=APP))
    assert st()["selected_dog_applied"] is False
    v0 = st()["state_version"]
    robot.selected = ELSA
    robot.report = {"selected_dog_id": ELSA, "selected_dog_changed_locally": False}

    async def main():
        robot.connect()
        await asyncio.sleep(3.3)      # past the 3 s fallback: it must not send either
    run(main())
    s = st()
    assert s["selected_dog_id"] == ELSA and s["selected_dog_applied"] is True
    assert robot.sent == []
    assert s["state_version"] == v0 + 2                      # connect (+1), applied (+1)
    assert robot.states()[-1]["selected_dog_applied"] is True
    print(f"R6-d sent={robot.sent} applied={s['selected_dog_applied']} version {v0}->{s['state_version']}")
