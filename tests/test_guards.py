"""Settings guards: quiet-hours widen-only, treat cap lower-only, treat cap enforcement, command allowlist."""
import asyncio

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.services import robot_control, settings_service

APP = Actor(kind="app", id="user_t1", user_id="user_t1")
CONN = Actor(kind="connector", id="claude", user_id="user_t1")
AGENT = Actor(kind="agent", id="agent", user_id="user_t1")


def _put(actor, key, value):
    return asyncio.run(settings_service.put_setting("dog_t1", key, value, actor))


def test_quiet_hours_widen_only(temp_db):
    _put(APP, "quiet_hours", {"enabled": True, "start": "22:00", "end": "07:00"})
    with pytest.raises(HTTPException) as e:
        _put(CONN, "quiet_hours", {"enabled": True, "start": "23:00", "end": "07:00"})
    assert e.value.status_code == 403 and e.value.detail["guard"] == "quiet_hours_widen_only"
    with pytest.raises(HTTPException):
        _put(CONN, "quiet_hours", {"enabled": False, "start": "22:00", "end": "07:00"})
    row = _put(CONN, "quiet_hours", {"enabled": True, "start": "21:00", "end": "08:00"})
    assert row["value"]["start"] == "21:00" and row["version"] == 2
    # the app may narrow again
    assert _put(APP, "quiet_hours", {"enabled": True, "start": "23:30", "end": "06:00"})["version"] == 3
    audit = settings_service.list_settings_audit("dog_t1") if hasattr(settings_service, "list_settings_audit") else None


def test_treat_cap_lower_only_for_connectors(temp_db):
    _put(APP, "treat_schedule", {"daily_cap": 5, "default_multiplier": 1.0, "max_per_hour": 3, "windows": []})
    with pytest.raises(HTTPException) as e:
        _put(CONN, "treat_schedule", {"daily_cap": 8, "default_multiplier": 1.0, "max_per_hour": 3, "windows": []})
    assert e.value.detail["guard"] == "treat_cap_lower_only"
    assert _put(CONN, "treat_schedule", {"daily_cap": 3, "default_multiplier": 1.0, "max_per_hour": 3, "windows": []})["value"]["daily_cap"] == 3


def test_treat_cap_enforced(temp_db):
    _put(APP, "treat_schedule", {"daily_cap": 2, "default_multiplier": 1.0, "max_per_hour": 10, "windows": []})
    g1 = settings_service.check_treat_cap("dog_t1", AGENT)
    g2 = settings_service.check_treat_cap("dog_t1", AGENT)
    assert g1["treats_used"] == 1 and g2["treats_used"] == 2 and g1["command_id"] != g2["command_id"]
    with pytest.raises(HTTPException) as e:
        settings_service.check_treat_cap("dog_t1", AGENT)
    assert e.value.status_code == 429 and e.value.detail["code"] == "treat_cap_reached"


def test_minute_set_wraps_midnight():
    assert settings_service.minute_set("22:00", "07:00") <= settings_service.minute_set("21:00", "08:00")
    assert not settings_service.minute_set("22:00", "07:00") <= settings_service.minute_set("23:00", "07:00")
    assert 0 in settings_service.minute_set("22:00", "07:00")


def test_command_allowlist(temp_db):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("webrtc_request", {}, CONN, "dog_t1")
    assert e.value.detail["code"] == "command_not_allowed"
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("take_photo", {}, CONN, "dog_t1")
    assert e.value.detail["code"] == "command_not_allowed"
    msg, _ = robot_control.build_robot_message("take_photo", {"with_hud": True}, AGENT, "dog_t1")
    assert msg["command"] == "take_photo" and msg["data"]["with_hud"] is True and msg["command_id"]
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("set_mode", {"mode": "manual"}, APP, "dog_t1")
    assert e.value.detail["code"] == "mode_not_allowed"
    msg, _ = robot_control.build_robot_message("standby", {}, CONN, "dog_t1")
    assert msg["command"] == "set_mode" and msg["data"]["mode"] == "idle"
    msg, _ = robot_control.build_robot_message("play_sound", {"sound_id": "chime"}, CONN, "dog_t1")
    assert msg["command"] == "audio" and msg["data"]["file"] == "chime.wav"
    msg, _ = robot_control.build_robot_message("play_music", {"track": "song_calm_01"}, Actor(kind="relay", id="trigger", user_id="user_t1"), "dog_t1")
    assert msg["data"]["url"].endswith("/song_calm_01")
    for name in robot_control.NEVER_EMIT:
        assert name not in {"dispense_treat", "set_mode", "audio", "play_music", "play_sound", "start_mission", "take_photo", "go_to", "run_trick"}
