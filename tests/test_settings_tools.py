"""set_treat_schedule and set_notify_rule for connectors and the voice agent (settings scope, 2026-11-08)."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.auth import Actor
from app.mcp_server import authz
from app.services import settings_service, settings_tools
from app.services.relay_client import RelayRefusal

AGENT = Actor(kind="agent", id="agent", user_id="user_t1")
OWNER = Actor(kind="app", id="user_t1", user_id="user_t1")


class LocalClient:
    """RelayClient stand-in that goes through settings_service (and its guards) as `actor`."""

    def __init__(self, actor=AGENT):
        self.actor = actor

    async def get_setting(self, dog_id, key):
        return settings_service.get_setting(dog_id, key)

    async def put_setting(self, dog_id, key, value, expected_version=None):
        try:
            return await settings_service.put_setting(dog_id, key, value, self.actor, expected_version)
        except settings_service.GuardDenied as e:
            raise RelayRefusal(403, e.guard, {"reason": e.reason})
        except HTTPException as e:
            d = e.detail if isinstance(e.detail, dict) else {"detail": e.detail}
            raise RelayRefusal(e.status_code, str(d.get("code") or e.status_code), d)


def run(c):
    return asyncio.run(c)


def treat(windows, dm=None):
    return run(settings_tools.set_treat_schedule(LocalClient(), "dog_t1", windows, dm))


def rule(event_type, **kw):
    return run(settings_tools.set_notify_rule(LocalClient(), "dog_t1", event_type, **kw))


# ---- set_treat_schedule ----

def test_treat_windows_written_and_read_back(temp_db):
    r = treat([{"start": "17:00", "end": "19:00", "multiplier": 2}, {"start": "08:00", "end": "09:30", "multiplier": 0.5}], 1.5)
    assert r["ok"] and r["windows"] == [{"start": "08:00", "end": "09:30", "multiplier": 0.5},
                                         {"start": "17:00", "end": "19:00", "multiplier": 2.0}]
    assert r["default_multiplier"] == 1.5 and r["previous_windows"] == []
    saved = settings_service.get_value("dog_t1", "treat_schedule")
    assert saved["windows"] == r["windows"] and saved["default_multiplier"] == 1.5


def test_treat_schedule_never_touches_caps(temp_db):
    run(settings_service.put_setting("dog_t1", "treat_schedule",
                                     {"daily_cap": 4, "max_per_hour": 2, "default_multiplier": 1, "windows": []}, OWNER))
    r = treat([{"start": "22:00", "end": "02:00", "multiplier": 3}])     # wraps midnight
    assert r["ok"] and r["daily_cap"] == 4 and r["default_multiplier"] == 1
    saved = settings_service.get_value("dog_t1", "treat_schedule")
    assert saved["daily_cap"] == 4 and saved["max_per_hour"] == 2
    assert "daily_cap" not in inspect.signature(settings_tools.set_treat_schedule).parameters


def test_empty_list_clears_windows(temp_db):
    treat([{"start": "08:00", "end": "09:00", "multiplier": 2}])
    r = treat([])
    assert r["ok"] and r["windows"] == [] and r["previous_windows"][0]["start"] == "08:00"


@pytest.mark.parametrize("windows,reason", [
    ([{"start": "8:00", "end": "09:00", "multiplier": 1}], "invalid_window_time"),
    ([{"start": "24:00", "end": "09:00", "multiplier": 1}], "invalid_window_time"),
    ([{"start": "08:00", "end": "08:00", "multiplier": 1}], "empty_window"),
    ([{"start": "08:00", "end": "09:00", "multiplier": 4}], "invalid_multiplier"),
    ([{"start": "08:00", "end": "09:00"}], "invalid_multiplier"),
    ([{"start": "08:00", "end": "10:00", "multiplier": 1}, {"start": "09:00", "end": "11:00", "multiplier": 2}], "windows_overlap"),
    ([{"start": "23:00", "end": "01:00", "multiplier": 1}, {"start": "00:30", "end": "02:00", "multiplier": 2}], "windows_overlap"),
    ([{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "multiplier": 1} for h in range(13)], "too_many_windows"),
    ("08:00-09:00", "invalid_windows"),
])
def test_bad_windows_refused_and_nothing_saved(temp_db, windows, reason):
    r = treat(windows)
    assert r["refused"] and r["reason"] == reason
    assert settings_service.get_setting("dog_t1", "treat_schedule")["version"] == 0


def test_bad_default_multiplier(temp_db):
    r = treat([], -1)
    assert r["refused"] and r["reason"] == "invalid_multiplier"


# ---- set_notify_rule ----

def test_rule_appended_with_matching_app_type(temp_db):
    before = len(settings_service.get_value("dog_t1", "notify_rules")["rules"])
    r = rule("bark_episode", min_duration_s=30, severity="med", attach="still", cooldown_s=600)
    assert r["ok"] and r["rule_count"] == before + 1 and r["max_rules"] == 10
    x = r["rule"]
    assert x["app_type"] == "bark" and x["attach"] == "still" and x["cooldown_s"] == 600
    assert x["when"] == {"min_duration_s": 30.0, "severity_in": ["med", "high"]} and x["override_quiet"] is False
    rules = settings_service.get_value("dog_t1", "notify_rules")["rules"]
    assert rules[-1]["id"] == x["id"] and rules[0]["id"] == "bark_long"          # existing rules kept


@pytest.mark.parametrize("event_type,app_type", [("treat", "treatDispensed"), ("mission_run", "missionCompleted"),
                                                 ("settle", "alert"), ("departure", "alert"), ("pacing", "alert")])
def test_app_type_follows_event_type(temp_db, event_type, app_type):
    r = rule(event_type)
    assert r["ok"] and r["rule"]["app_type"] == app_type and r["rule"]["when"] == {} and r["rule"]["attach"] == "none"


def test_max_ten_rules(temp_db):
    n = len(settings_service.get_value("dog_t1", "notify_rules")["rules"])
    for _ in range(10 - n):
        assert rule("arrival")["ok"]
    r = rule("arrival")
    assert r["refused"] and r["reason"] == "notify_rules_limit" and r["rule_count"] == 10
    assert len(settings_service.get_value("dog_t1", "notify_rules")["rules"]) == 10


@pytest.mark.parametrize("kw,reason", [
    ({"event_type": "status"}, "invalid_event_type"),
    ({"event_type": "bark_episode", "attach": "video"}, "invalid_attach"),
    ({"event_type": "bark_episode", "severity": "extreme"}, "invalid_severity"),
    ({"event_type": "pacing", "severity": "high"}, "severity_only_for_bark_episode"),
    ({"event_type": "pacing", "min_duration_s": -5}, "invalid_min_duration_s"),
    ({"event_type": "pacing", "cooldown_s": 90000}, "invalid_cooldown_s"),
])
def test_bad_rules_refused(temp_db, kw, reason):
    r = run(settings_tools.set_notify_rule(LocalClient(), "dog_t1", **kw))
    assert r["refused"] and r["reason"] == reason


# ---- registration, scope, voice agent ----

def test_scope_registration_and_not_strict():
    for name in ("set_treat_schedule", "set_notify_rule"):
        assert authz.TOOL_SCOPES[name] == "settings"
    from app.mcp_server import tools as mt
    src = inspect.getsource(mt)
    assert '@mcp.tool(name="set_treat_schedule"' in src and '@mcp.tool(name="set_notify_rule"' in src
    from app.voice_agent import tools as vt
    t = {x["name"]: x for x in vt.TOOLS}
    assert not t["set_treat_schedule"].get("strict") and not t["set_notify_rule"].get("strict")
    assert "daily_cap" not in t["set_treat_schedule"]["input_schema"]["properties"]
    assert t["set_notify_rule"]["input_schema"]["required"] == ["dog_id", "event_type"]


def test_agent_dispatch(temp_db, monkeypatch):
    from app.voice_agent import tools as vt
    ctx = vt.ToolContext(user_id="user_t1", source="phone")
    ctx.client = LocalClient()
    r = run(vt.dispatch(ctx, "set_treat_schedule", {"dog_id": "dog_t1",
                                                    "windows": [{"start": "18:00", "end": "19:00", "multiplier": 2}]}))
    assert r["ok"] and r["windows"][0]["start"] == "18:00"
    r = run(vt.dispatch(ctx, "set_notify_rule", {"dog_id": "dog_t1", "event_type": "settle"}))
    assert r["ok"] and r["rule"]["app_type"] == "alert"


def test_agent_readback():
    from app.voice_agent.brain import Brain
    ch = {"tool": "set_treat_schedule", "result": {"ok": True, "default_multiplier": 1.0,
          "windows": [{"start": "08:00", "end": "09:30", "multiplier": 0.5}, {"start": "17:00", "end": "19:00", "multiplier": 2.0}]}}
    assert Brain._readback_sentence(ch) == ("Treat windows are now 08:00 to 09:30 at 0.5x, 17:00 to 19:00 at 2x "
                                            "and 1x the rest of the day.")
    assert "5:00" in Brain._readback_tokens(ch) and all(isinstance(t, str) for t in Brain._readback_tokens(ch))
    ch = {"tool": "set_notify_rule", "result": {"ok": True, "rule": {"event_type": "bark_episode", "attach": "still",
          "cooldown_s": 600, "when": {"min_duration_s": 30.0, "severity_in": ["med", "high"]}}}}
    assert Brain._readback_sentence(ch) == ("You'll now get a notification for each bark episode, med severity or higher, "
                                            "at least 30 seconds, at most once every 600 seconds, with a photo.")
