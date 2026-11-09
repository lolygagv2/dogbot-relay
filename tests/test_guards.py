"""Settings guards: quiet-hours widen-only, treat caps changeable by every actor, treat cap enforcement, command allowlist."""
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


TS = {"default_multiplier": 1.0, "windows": []}


@pytest.fixture()
def pushes(monkeypatch):
    sent = []

    async def fake_push(**kw):
        sent.append(kw)
        return "sent"
    from app.services import push_service
    monkeypatch.setattr(push_service, "send_push", fake_push)
    return sent


def test_treat_caps_changeable_by_app_connector_and_agent(temp_db, pushes):
    """2026-11-09: no actor is stuck with the default 10/day; AIs raising a limit alert the owner."""
    assert _put(APP, "treat_schedule", {**TS, "daily_cap": 5, "max_per_hour": 3})["value"]["daily_cap"] == 5
    assert pushes == []                                            # the owner's own change: no alert
    assert _put(CONN, "treat_schedule", {**TS, "daily_cap": 8, "max_per_hour": 3})["value"]["daily_cap"] == 8
    assert len(pushes) == 1 and "5 → 8" in pushes[0]["body"] and pushes[0]["data"]["kind"] == "treat_limit_raised"
    assert _put(AGENT, "treat_schedule", {**TS, "daily_cap": 100, "max_per_hour": 10})["value"]["daily_cap"] == 100
    assert len(pushes) == 2 and "voice assistant" in pushes[1]["body"] and "hourly treat limit 3 → 10" in pushes[1]["body"]
    assert _put(CONN, "treat_schedule", {**TS, "daily_cap": 4, "max_per_hour": 2})["value"]["daily_cap"] == 4
    assert len(pushes) == 2                                        # lowering: no alert
    with pytest.raises(HTTPException) as e:                        # bounds still apply to everyone
        _put(CONN, "treat_schedule", {**TS, "daily_cap": 101, "max_per_hour": 2})
    assert e.value.status_code == 422


def test_robot_may_write_treat_schedule_only(temp_db):
    from app.models import SettingPut
    from app.routers import dog_settings
    robot = Actor(kind="robot", id="wimz_t1", user_id="user_t1", device_id="wimz_t1")
    row = asyncio.run(dog_settings.put_setting(
        "dog_t1", "treat_schedule", SettingPut(value={**TS, "daily_cap": 100, "max_per_hour": 3}), robot))
    assert row["value"]["daily_cap"] == 100
    assert settings_service.check_treat_cap("dog_t1", AGENT)["treat_cap"] == 100
    with pytest.raises(HTTPException) as e:
        asyncio.run(dog_settings.put_setting(
            "dog_t1", "quiet_hours", SettingPut(value={"enabled": True, "start": "22:00", "end": "07:00"}), robot))
    assert e.value.detail["code"] == "robot_read_only"
    other = Actor(kind="robot", id="wimz_x", user_id="someone_else", device_id="wimz_x")
    with pytest.raises(HTTPException) as e:
        asyncio.run(dog_settings.put_setting(
            "dog_t1", "treat_schedule", SettingPut(value={**TS, "daily_cap": 50, "max_per_hour": 3}), other))
    assert e.value.detail["code"] == "dog_not_owned_by_device_owner"


def _limit_msg(n, enabled=True):
    return {"type": "command", "command": "mission_config", "timestamp": 1,
            "data": {"daily_limit_enabled": enabled, "daily_limit": n}}


def test_app_settings_daily_limit_mirrored_debounced(temp_db):
    """The app's Settings slider sends mission_config {daily_limit_enabled, daily_limit} per step;
    only the final value lands in every owned dog's treat_schedule. Toggle off = unlimited."""
    import app.database as db
    db.create_dog(dog_id="dog_t2", name="Bea", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t2", "owner")

    async def drag(values, enabled=True):
        for v in values:
            settings_service.sync_app_daily_limit("user_t1", _limit_msg(v, enabled), delay_s=0.05)
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)

    asyncio.run(drag([70, 65, 60, 55, 50, 45, 40, 35]))
    for dog in ("dog_t1", "dog_t2"):
        row = settings_service.get_setting(dog, "treat_schedule")
        assert row["value"]["daily_cap"] == 35 and row["version"] == 1        # one write, final value
        assert row["value"]["max_per_hour"] == 0                              # untouched (default: off)
    asyncio.run(drag([35], enabled=False))                                    # toggle off
    off = settings_service.get_value("dog_t1", "treat_schedule")
    assert off["daily_cap_enabled"] is False and off["daily_cap"] == 35       # number kept for "on"
    assert settings_service.effective_daily_cap(off) is None
    asyncio.run(drag([35]))                                                   # toggle back on
    on = settings_service.get_value("dog_t2", "treat_schedule")
    assert on["daily_cap_enabled"] is True and settings_service.effective_daily_cap(on) == 35
    # real missions (no daily_limit fields) are ignored
    assert settings_service.sync_app_daily_limit("user_t1", {"command": "mission_config",
                                                             "data": {"mission_id": "m1", "max_treats": 5}}) is None
    assert settings_service.app_daily_limit_from_message(_limit_msg(500)) == {"daily_cap_enabled": True, "daily_cap": 100}


def test_unlimited_daily_cap_enforces_nothing_but_hourly(temp_db, pushes):
    _put(APP, "treat_schedule", {**TS, "daily_cap": 2, "max_per_hour": 0})
    settings_service.check_treat_cap("dog_t1", AGENT)
    settings_service.check_treat_cap("dog_t1", AGENT)
    with pytest.raises(HTTPException):
        settings_service.check_treat_cap("dog_t1", AGENT)                    # capped at 2
    _put(APP, "treat_schedule", {**TS, "daily_cap": 2, "daily_cap_enabled": False, "max_per_hour": 0})
    for _ in range(5):
        g = settings_service.check_treat_cap("dog_t1", AGENT)                # unlimited
    assert g["treat_cap"] is None and g["treats_used"] == 7
    # an assistant turning the cap off counts as raising it: owner alerted
    _put(APP, "treat_schedule", {**TS, "daily_cap": 2, "max_per_hour": 0})
    _put(CONN, "treat_schedule", {**TS, "daily_cap": 2, "daily_cap_enabled": False, "max_per_hour": 0})
    assert "daily treat cap 2 → unlimited" in pushes[-1]["body"]
    # hourly: 0 means no limit, so 0 -> 5 is a lowering (no alert) and 5 -> 0 is a raise
    n = len(pushes)
    _put(APP, "treat_schedule", {**TS, "daily_cap": 2, "max_per_hour": 0})
    _put(CONN, "treat_schedule", {**TS, "daily_cap": 2, "max_per_hour": 5})
    assert len(pushes) == n
    _put(CONN, "treat_schedule", {**TS, "daily_cap": 2, "max_per_hour": 0})
    assert len(pushes) == n + 1 and "hourly treat limit 5 → unlimited" in pushes[-1]["body"]


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


def test_connector_take_photo_guards(temp_db, monkeypatch):
    """PHOTO ACCESS: connector snapshots are HUD-less and rate limited per assistant."""
    robot_control._last_photo.clear()
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: False)
    msg, _ = robot_control.build_robot_message("take_photo", {"with_hud": True}, CONN, "dog_t1")
    assert msg["command"] == "take_photo" and msg["data"]["with_hud"] is False
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("take_photo", {}, CONN, "dog_t1")
    assert e.value.status_code == 429 and e.value.detail["code"] == "camera_rate"
    other = Actor(kind="connector", id="chatgpt", user_id="user_t1")
    robot_control.build_robot_message("take_photo", {}, other, "dog_t1")      # per-assistant limit
    # quiet hours no longer block snapshots (they suppress only music, speech, non-urgent pushes)
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: True)
    robot_control._last_photo.clear()
    assert robot_control.build_robot_message("take_photo", {}, CONN, "dog_t1")[0]["command"] == "take_photo"
    # the agent is not subject to the connector rule
    msg, _ = robot_control.build_robot_message("take_photo", {"with_hud": True}, AGENT, "dog_t1")
    assert msg["data"]["with_hud"] is True


# ---- quiet hours: opt-in, narrow scope (2026-11-07) ----

def test_quiet_hours_off_by_default(temp_db):
    assert settings_service.get_value("dog_t1", "quiet_hours") == {"enabled": False, "start": "22:00", "end": "07:00"}
    assert settings_service.in_quiet_hours("dog_t1") is False


def test_connector_may_turn_on_or_widen_never_off_or_narrow(temp_db):
    row = _put(CONN, "quiet_hours", {"enabled": True, "start": "23:00", "end": "06:00"})   # off -> on: ok
    assert row["value"]["enabled"] is True
    for bad in ({"enabled": True, "start": "23:30", "end": "06:00"},    # narrow
                {"enabled": False, "start": "23:00", "end": "06:00"},   # turn off
                {"enabled": False, "start": "20:00", "end": "09:00"}):  # off, even if "wider"
        with pytest.raises(HTTPException) as e:
            _put(CONN, "quiet_hours", bad)
        assert e.value.detail["guard"] == "quiet_hours_widen_only"
    assert _put(AGENT, "quiet_hours", {"enabled": True, "start": "21:00", "end": "07:00"})["value"]["start"] == "21:00"
    assert _put(APP, "quiet_hours", {"enabled": False, "start": "21:00", "end": "07:00"})["value"]["enabled"] is False


def test_quiet_hours_hold_back_only_non_urgent_pushes(temp_db, monkeypatch):
    from app.services import notify_service, push_service
    sent = []

    async def fake_push(**kw):
        sent.append(kw["app_type"])
        return "sent"

    monkeypatch.setattr(push_service, "send_push", fake_push)
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: True)
    bark = {"event_id": "b1", "type": "bark_episode", "dog_id": "dog_t1", "ts": "2026-11-06T03:00:00+00:00",
            "duration_s": 120, "data": {"bark_count": 30, "severity": "high"}}
    settle = {"event_id": "s1", "type": "settle", "dog_id": "dog_t1", "ts": "2026-11-06T03:05:00+00:00",
              "duration_s": None, "data": {"settle_s": 300}}
    out_b = asyncio.run(notify_service.evaluate_notify_rules(bark, "user_t1"))
    out_s = asyncio.run(notify_service.evaluate_notify_rules(settle, "user_t1"))
    assert [o["rule_id"] for o in out_b] == ["bark_long"] and out_b[0]["outcome"] != "suppressed_quiet"
    assert sent == ["bark"]
    assert out_s == [{"rule_id": "settled", "outcome": "suppressed_quiet"}]


def test_music_refused_during_quiet_hours(temp_db, monkeypatch):
    relay = Actor(kind="relay", id="trigger", user_id="user_t1")
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: True)
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("play_music", {"track": "song_calm_01"}, relay, "dog_t1")
    assert e.value.detail["guard"] == "music_quiet_hours"
    monkeypatch.setattr(settings_service, "in_quiet_hours", lambda dog_id, local_dt=None: False)
    assert robot_control.build_robot_message("play_music", {"track": "song_calm_01"}, relay, "dog_t1")[0]["command"] == "play_music"


@pytest.mark.parametrize("target", ["dog", "dock"])
def test_go_to_dog_or_dock(temp_db, target):
    msg, _ = robot_control.build_robot_message("go_to", {"target": target}, CONN, "dog_t1")
    assert msg["command"] == "go_to" and msg["data"]["target"] == target


@pytest.mark.parametrize("target", ["bed", "kitchen", "door", "crate", "living_room", "owner", ""])
def test_go_to_named_spots_rejected(temp_db, target):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("go_to", {"target": target}, AGENT, "dog_t1")
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "unknown_target" and e.value.detail["allowed"] == ["dock", "dog"]


@pytest.mark.parametrize("action,amount,unit", [
    ("forward", 15, "cm"), ("backward", 5, "cm"), ("forward", 30, "cm"),
    ("turn_left", 15, "deg"), ("turn_right", 90, "deg"),
])
def test_move_in_range(temp_db, action, amount, unit):
    msg, _ = robot_control.build_robot_message("move", {"action": action, "amount": amount}, CONN, "dog_t1")
    assert msg["command"] == "move"
    assert msg["data"] == {"action": action, "amount": amount, "unit": unit, "speed": "creep",
                           "source": "connector", "command_id": msg["command_id"]}


@pytest.mark.parametrize("action,default", [("forward", 15), ("backward", 15), ("turn_left", 30), ("turn_right", 30)])
def test_move_default_amount(temp_db, action, default):
    msg, _ = robot_control.build_robot_message("move", {"action": action}, AGENT, "dog_t1")
    assert msg["data"]["amount"] == default


@pytest.mark.parametrize("data,code", [
    ({"action": "spin", "amount": 10}, "unknown_move_action"),
    ({"action": "", "amount": 10}, "unknown_move_action"),
    ({"action": "forward", "amount": 4}, "move_amount_out_of_range"),
    ({"action": "forward", "amount": 31}, "move_amount_out_of_range"),
    ({"action": "turn_left", "amount": 91}, "move_amount_out_of_range"),
    ({"action": "turn_right", "amount": 14}, "move_amount_out_of_range"),
    ({"action": "forward", "amount": "far"}, "move_amount_out_of_range"),
    ({"action": "forward", "amount": True}, "move_amount_out_of_range"),
])
def test_move_rejected(temp_db, data, code):
    with pytest.raises(HTTPException) as e:
        robot_control.build_robot_message("move", data, APP, "dog_t1")
    assert e.value.status_code == 422 and e.value.detail["code"] == code


def test_move_needs_physical_scope():
    from app.mcp_server import authz
    assert authz.TOOL_SCOPES["move"] == "physical" and authz.TOOL_SCOPES["go_to"] == "physical"


def test_move_tool_result_shapes():
    r = robot_control.move_tool_result
    assert r("forward", {"simulated": True}) == {"ok": False, "reason": "robot_offline_demo", "simulated": True}
    assert r("forward", {"timed_out": True, "command_id": "c"}) == {"ok": False, "reason": "move_timed_out", "command_id": "c"}
    assert r("forward", {"command_id": "c", "result": {"ok": False, "error": "cliff",
                                                        "result": {"blocked": True, "reason": "cliff"}}}) == \
        {"ok": False, "action": "forward", "command_id": "c", "blocked": True, "reason": "cliff", "error": "cliff"}


def test_tool_descriptions_and_instructions():
    from app.mcp_server import server
    from app.voice_agent import prompts, tools as vt
    by_name = {t["name"]: t for t in vt.TOOLS}
    assert by_name["go_to"]["description"] == "Send the robot to the dog or back to the dock; it finishes on its own."
    assert by_name["move"]["description"] == "One small supervised step at creep speed; take a photo, decide one step, repeat."
    assert by_name["go_to"]["input_schema"]["properties"]["target"]["enum"] == ["dog", "dock"]
    rule = "Stop and explain after 8 move cycles, or after a full 360-degree search finds nothing."
    assert rule in prompts.SYSTEM_PROMPT and rule in server.INSTRUCTIONS
