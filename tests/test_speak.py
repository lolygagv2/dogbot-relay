"""speak (2026-11-07): connector + agent make the robot talk in the WIM-Z voice only.

Guards: physical scope (MCP), connector/agent callers only, <= 280 chars, refused during quiet
hours, 6 per minute per assistant. Delivery: play_sound {url, kind: "tts"} to the owner's robot.
"""
import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.database as db
from app.auth import Actor
from app.config import get_settings
from app.connection_manager import get_connection_manager
from app.mcp_server import authz
from app.models import SpeakRequest
from app.services import robot_control, settings_service
from app.voice_agent import tools as agent_tools
from app.voice_agent import tts

CONN = Actor(kind="connector", id="claude", user_id="user_t1")
CONN2 = Actor(kind="connector", id="chatgpt", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")
APP = Actor(kind="app", id="user_t1", user_id="user_t1")


class _FakeResp:
    status_code = 200
    content = b"ID3fake-mp3"
    text = ""


@pytest.fixture
def rig(temp_db, tmp_path, monkeypatch):
    """Owner's robot online, sends captured, ElevenLabs faked, clips written to tmp."""
    s = get_settings()
    monkeypatch.setattr(s, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(s, "elevenlabs_voice_id", "WIMZ_VOICE")
    monkeypatch.setattr(s, "agent_audio_dir", str(tmp_path))
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: False)
    robot_control._speak_log.clear()
    db.create_device_pairing("user_t1", "robot_t")
    manager = get_connection_manager()
    manager.robot_connections["robot_t"] = object()
    sent, tts_urls = [], []

    async def fake_send(device_id, message):
        sent.append((device_id, message))
        return True

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            tts_urls.append(url)
            return _FakeResp()

    monkeypatch.setattr(manager, "send_to_robot", fake_send)
    monkeypatch.setattr(tts.httpx, "AsyncClient", FakeClient)
    yield {"sent": sent, "tts_urls": tts_urls}
    manager.robot_connections.pop("robot_t", None)


def _speak(text, actor=CONN):
    return asyncio.run(robot_control.speak("dog_t1", text, actor))


def test_speak_sends_tts_play_sound_in_wimz_voice(rig):
    out = _speak("  Hi Rex,   good boy!  ")
    assert out["ok"] and out["delivered"] and out["chars"] == len("Hi Rex, good boy!")
    [(device, msg)] = rig["sent"]
    assert device == "robot_t" and msg["command"] == "play_sound"
    assert msg["data"]["kind"] == "tts" and msg["data"]["url"].endswith(f"/agent/audio/{out['clip_id']}.mp3")
    # the only voice ever requested is the configured WIM-Z voice
    assert rig["tts_urls"] == ["https://api.elevenlabs.io/v1/text-to-speech/WIMZ_VOICE"]


def test_speak_text_length_and_no_voice_field(rig):
    assert _speak("x" * 280)["ok"]
    for bad in ("x" * 281, "   "):
        with pytest.raises(HTTPException) as e:
            _speak(bad)
        assert e.value.status_code == 422 and e.value.detail["code"] == "speak_text_invalid"
    with pytest.raises(ValidationError):
        SpeakRequest.model_validate({"text": "hi", "voice_id": "someone_else"})   # no voice choice
    with pytest.raises(ValidationError):
        SpeakRequest.model_validate({"text": "x" * 281})


def test_speak_refused_during_quiet_hours(rig, monkeypatch):
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: True)
    with pytest.raises(HTTPException) as e:
        _speak("hello")
    assert e.value.status_code == 403 and e.value.detail["guard"] == "speak_quiet_hours"
    assert rig["sent"] == [] and rig["tts_urls"] == []          # refused before any TTS spend


def test_speak_rate_limit_six_per_minute_per_assistant(rig):
    for i in range(6):
        assert _speak(f"line {i}")["speaks_left_this_minute"] == 5 - i
    with pytest.raises(HTTPException) as e:
        _speak("seventh")
    assert e.value.status_code == 429 and e.value.detail["code"] == "speak_rate"
    assert _speak("other assistant", CONN2)["ok"]               # separate budget
    assert _speak("agent too", AGENT)["ok"]


def test_speak_only_for_connector_and_agent(rig):
    with pytest.raises(HTTPException) as e:
        _speak("hi", APP)
    assert e.value.status_code == 403 and e.value.detail["code"] == "speak_not_allowed"
    # callers can never send play_tts themselves except the agent's own path
    with pytest.raises(HTTPException):
        robot_control.build_robot_message("play_tts", {"url": "https://evil.example/x.mp3"}, CONN, "dog_t1")


def test_speak_is_physical_scope_and_agent_physical_tool():
    assert authz.TOOL_SCOPES["speak"] == "physical"
    assert "speak" in agent_tools.PHYSICAL_TOOLS
    assert any(t["name"] == "speak" for t in agent_tools.TOOLS)
