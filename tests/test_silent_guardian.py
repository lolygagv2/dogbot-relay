"""set_silent_guardian: connectors and the voice agent turn SG mode on/off (2026-11-07)."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.mcp_server import authz
from app.services import robot_control as rc

CONN = Actor(kind="connector", id="claude", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")


@pytest.mark.parametrize("actor", [CONN, AGENT])
@pytest.mark.parametrize("on,mode", [(True, "silent_guardian"), (False, "idle")])
def test_on_off_builds_set_mode(temp_db, actor, on, mode):
    msg, _ = rc.build_robot_message("set_mode", rc.sg_mode_command(on), actor, "dog_t1")
    assert msg["command"] == "set_mode"
    assert msg["data"]["mode"] == mode


def test_only_two_modes_reachable():
    assert {rc.sg_mode_command(True)["mode"], rc.sg_mode_command(False)["mode"]} == {"silent_guardian", "idle"}


def test_manual_mode_still_refused(temp_db):
    with pytest.raises(HTTPException) as e:
        rc.build_robot_message("set_mode", {"mode": "manual"}, CONN, "dog_t1")
    assert e.value.status_code == 403


def test_result_shape():
    r = rc.sg_mode_tool_result(True, {"delivered": True, "simulated": False, "command_id": "c1"})
    assert r["ok"] and r["silent_guardian"] == "on" and r["mode"] == "silent_guardian" and r["delivered"]
    assert "Sent to the robot" in r["message"]
    r = rc.sg_mode_tool_result(False, {"delivered": False, "simulated": True})
    assert r["silent_guardian"] == "off" and r["simulated"] and "Simulated" in r["message"]


def test_offline_robot_is_refused(temp_db):
    with pytest.raises(HTTPException) as e:
        asyncio.run(rc.dispatch("dog_t1", "set_mode", rc.sg_mode_command(True), CONN))
    assert e.value.status_code in (404, 409)


def test_scope_and_registration():
    assert authz.TOOL_SCOPES["set_silent_guardian"] == "settings"
    from app.mcp_server import tools as mt
    assert '@mcp.tool(name="set_silent_guardian"' in inspect.getsource(mt)
    from app.voice_agent import tools as vt
    t = {x["name"]: x for x in vt.TOOLS}["set_silent_guardian"]
    assert t["input_schema"]["required"] == ["dog_id", "on"]
    assert "set_silent_guardian" not in vt.PHYSICAL_TOOLS


@pytest.mark.parametrize("on,state", [(True, "on"), (False, "off")])
def test_agent_readback(on, state):
    from app.voice_agent.brain import Brain
    change = {"tool": "set_silent_guardian", "result": rc.sg_mode_tool_result(on, {"delivered": True})}
    assert Brain._readback_sentence(change) == f"I've told the robot to turn Silent Guardian {state}."
    toks = Brain._readback_tokens(change)
    assert any(t in f"Done, I turned Silent Guardian {state} for Elsa." for t in toks)
    assert any(t in f"Silent Guardian is now {state}." for t in toks)
    assert not any(t in "Silent Guardian sensitivity is high." for t in toks)
