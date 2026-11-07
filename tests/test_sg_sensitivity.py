"""set_sg_sensitivity for connectors and the voice agent (settings scope, 2026-11-07)."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.mcp_server import authz
from app.services import settings_service

CONN = Actor(kind="connector", id="claude", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")


def put(value, actor):
    return asyncio.run(settings_service.put_setting("dog_t1", "sg_sensitivity", value, actor))


@pytest.mark.parametrize("actor", [CONN, AGENT])
def test_connector_and_agent_may_change_sensitivity(temp_db, actor):
    row = put({"level": "high", "min_episode_s": 3, "escalate_after_s": 30}, actor)
    assert row["value"] == {"level": "high", "min_episode_s": 3, "escalate_after_s": 30}
    assert settings_service.get_value("dog_t1", "sg_sensitivity")["level"] == "high"


@pytest.mark.parametrize("bad", [{"level": "max"}, {"level": "low", "min_episode_s": 0},
                                 {"level": "low", "escalate_after_s": 4000}])
def test_invalid_values_rejected(temp_db, bad):
    with pytest.raises(HTTPException) as e:
        put(bad, CONN)
    assert e.value.status_code == 422


def test_scope_and_registration():
    assert authz.TOOL_SCOPES["set_sg_sensitivity"] == "settings"
    from app.mcp_server import tools as mt
    assert '@mcp.tool(name="set_sg_sensitivity"' in inspect.getsource(mt)
    from app.voice_agent import tools as vt
    t = {x["name"]: x for x in vt.TOOLS}["set_sg_sensitivity"]
    assert t["input_schema"]["properties"]["level"]["enum"] == ["low", "medium", "high"]


def test_agent_readback():
    from app.voice_agent.brain import Brain
    change = {"tool": "set_sg_sensitivity", "result": {"ok": True, "level": "high", "min_episode_s": 3,
                                                        "escalate_after_s": 30}}
    assert Brain._readback_sentence(change) == ("Silent Guardian sensitivity is now high: an episode starts "
                                                "after 3 seconds and escalates after 30 seconds.")
    assert Brain._readback_tokens(change) == ["high"]
