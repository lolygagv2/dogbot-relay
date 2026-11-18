"""Treat-schedule windows and notification rules for connectors and the voice agent (2026-11-08).

Both tools sit in the `settings` scope and write through the relay's settings API as the
caller's internal actor, so the relay's own guards still apply on top of the checks here.

set_treat_schedule replaces `treat_schedule.windows` and/or sets default_multiplier, the
daily treat cap (0-100, or daily_cap_enabled=false for no daily cap) and the hourly treat limit
(0-60); anything omitted keeps its current value. The whole treat schedule is account-wide: the
relay copies it (limits, windows, default_multiplier) to all the owner's dogs. Raising either limit is allowed (since 2026-11-09) and pushes the owner an alert.
The result reads back the saved schedule and the previous limits.

set_music (2026-11-18) changes the calming-music plan: enabled, volume, and one trigger per call
(on_mode_start, after_treat, on_bark_episode or at_time) with its track and duration. The robot
runs the triggers; departure/arrival triggers no longer exist.

set_notify_rule appends one rule to `notify_rules`, with the app_type that matches its event
type, up to MAX_NOTIFY_RULES rules per dog (the owner's existing rules count).
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from pydantic import ValidationError

from app.models import (
    MUSIC_TRIGGER_KINDS, MusicAfterTreat, MusicAtTime, MusicOnBarkEpisode, MusicOnModeStart,
    MusicPlan, NotifyRule, TreatSchedule,
)
from app.services import settings_service
from app.services.relay_client import RelayClient
from app.services.settings_service import minute_set

MAX_TREAT_WINDOWS = 12
MULTIPLIER_MIN, MULTIPLIER_MAX = 0.0, 3.0
DAILY_CAP_MIN, DAILY_CAP_MAX = 0, 100      # = TreatSchedule.daily_cap bounds
HOURLY_MIN, HOURLY_MAX = 0, 60             # = TreatSchedule.max_per_hour bounds
MAX_NOTIFY_RULES = 10
MAX_SECONDS = 86400
NOTIFY_EVENT_TYPES: list[str] = list(NotifyRule.model_fields["event_type"].annotation.__args__)
ATTACH = ["none", "still", "clip"]
SEVERITIES = ["low", "med", "high"]   # bark_episode data.severity, lowest first
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# The push category the app expects for each event type; everything else is a generic alert.
APP_TYPE_FOR_EVENT = {"bark_episode": "bark", "treat": "treatDispensed", "mission_run": "missionCompleted"}

# Push text per event type; {placeholders} are filled by notify_service._fmt_vars.
_TEXT = {
    "bark_episode": ("{dog_name} is barking", "{duration_min} min, {bark_count} barks"),
    "intervention": ("WIM-Z stepped in", "{dog_name}: {kind}"),
    "settle": ("{dog_name} settled", "after {settle_min} min"),
    "pacing": ("{dog_name} is pacing", "{duration_min} min"),
    "treat": ("Treat given", "by {source}"),
    "music": ("Music for {dog_name}", "{track}"),
    "mission_run": ("Mission {result}", "{mission_type}"),
}


def _refuse(reason: str, **extra: Any) -> dict:
    return {"ok": False, "refused": True, "reason": reason, **extra}


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalise_windows(windows: Any) -> tuple[Optional[list[dict]], Optional[dict]]:
    """(windows, None) when valid, else (None, refusal). Windows may wrap midnight; no overlaps."""
    if not isinstance(windows, list):
        return None, _refuse("invalid_windows", hint="windows must be a list of {start, end, multiplier}")
    if len(windows) > MAX_TREAT_WINDOWS:
        return None, _refuse("too_many_windows", max=MAX_TREAT_WINDOWS, requested=len(windows))
    out: list[dict] = []
    covered: list[tuple[int, set[int]]] = []
    for i, w in enumerate(windows):
        w = w if isinstance(w, dict) else {}
        start, end, mult = str(w.get("start") or ""), str(w.get("end") or ""), _num(w.get("multiplier"))
        if not (_HM.match(start) and _HM.match(end)):
            return None, _refuse("invalid_window_time", window=i, hint="start and end are HH:MM, 24h local time")
        if start == end:
            return None, _refuse("empty_window", window=i, hint="start and end must differ")
        if mult is None or not MULTIPLIER_MIN <= mult <= MULTIPLIER_MAX:
            return None, _refuse("invalid_multiplier", window=i, min=MULTIPLIER_MIN, max=MULTIPLIER_MAX)
        mins = minute_set(start, end)
        for j, other in covered:
            if mins & other:
                return None, _refuse("windows_overlap", windows=[j, i])
        covered.append((i, mins))
        out.append({"start": start, "end": end, "multiplier": mult})
    return sorted(out, key=lambda x: x["start"]), None


def _int_in(v: Any, lo: int, hi: int) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None and n == int(n) and lo <= n <= hi else None


async def set_treat_schedule(client: RelayClient, dog_id: str, windows: Any = None,
                             default_multiplier: Optional[float] = None,
                             daily_cap: Any = None, max_per_hour: Any = None,
                             daily_cap_enabled: Optional[bool] = None) -> dict:
    """windows=None keeps the current windows; [] clears them."""
    new_windows = None
    if windows is not None:
        new_windows, bad = normalise_windows(windows)
        if bad:
            return bad
    if default_multiplier is not None:
        dm = _num(default_multiplier)
        if dm is None or not MULTIPLIER_MIN <= dm <= MULTIPLIER_MAX:
            return _refuse("invalid_multiplier", field="default_multiplier", min=MULTIPLIER_MIN, max=MULTIPLIER_MAX)
    if daily_cap is not None:
        cap = _int_in(daily_cap, DAILY_CAP_MIN, DAILY_CAP_MAX)
        if cap is None:
            return _refuse("invalid_daily_cap", min=DAILY_CAP_MIN, max=DAILY_CAP_MAX, requested=daily_cap)
    if max_per_hour is not None:
        hourly = _int_in(max_per_hour, HOURLY_MIN, HOURLY_MAX)
        if hourly is None:
            return _refuse("invalid_max_per_hour", min=HOURLY_MIN, max=HOURLY_MAX, requested=max_per_hour)
    if daily_cap_enabled is not None and not isinstance(daily_cap_enabled, bool):
        return _refuse("invalid_daily_cap_enabled", hint="true or false")
    if (new_windows is None and default_multiplier is None and daily_cap is None and max_per_hour is None
            and daily_cap_enabled is None):
        return _refuse("nothing_to_change",
                       hint="pass windows, default_multiplier, daily_cap, daily_cap_enabled or max_per_hour")
    cur_row = await client.get_setting(dog_id, "treat_schedule")
    cur = dict(cur_row.get("value") or {})
    new = dict(cur)
    if new_windows is not None:
        new["windows"] = new_windows
    if default_multiplier is not None:
        new["default_multiplier"] = dm
    if daily_cap is not None:
        new["daily_cap"] = cap
        if daily_cap_enabled is None:
            new["daily_cap_enabled"] = True        # naming a number means "cap at that number"
    if daily_cap_enabled is not None:
        new["daily_cap_enabled"] = daily_cap_enabled
    if max_per_hour is not None:
        new["max_per_hour"] = hourly
    row = await client.put_setting(dog_id, "treat_schedule", new, expected_version=cur_row.get("version"))
    v = row["value"]
    raised = bool(settings_service.treat_limits_raised(cur, v))
    out = {"ok": True, "windows": v.get("windows", []), "default_multiplier": v.get("default_multiplier"),
           # daily_cap None = unlimited (no daily cap)
           "daily_cap": settings_service.effective_daily_cap(v), "max_per_hour": v.get("max_per_hour"),
           "previous_daily_cap": settings_service.effective_daily_cap(cur),
           "previous_max_per_hour": TreatSchedule.model_validate(cur).max_per_hour,
           "previous_windows": cur.get("windows", []), "version": row.get("version")}
    if raised:
        out["owner_alerted"] = True
    return out


def build_notify_rule(event_type: str, min_duration_s: Any = None, severity: Optional[str] = None,
                      attach: Optional[str] = None, cooldown_s: Any = None) -> tuple[Optional[dict], Optional[dict]]:
    """(rule, None) when valid, else (None, refusal). severity means "at least": med -> med or high."""
    if event_type not in NOTIFY_EVENT_TYPES:
        return None, _refuse("invalid_event_type", allowed=NOTIFY_EVENT_TYPES)
    attach = attach or "none"
    if attach not in ATTACH:
        return None, _refuse("invalid_attach", allowed=ATTACH)
    when: dict[str, Any] = {}
    if min_duration_s is not None:
        d = _num(min_duration_s)
        if d is None or not 0 <= d <= MAX_SECONDS:
            return None, _refuse("invalid_min_duration_s", min=0, max=MAX_SECONDS)
        if d > 0:
            when["min_duration_s"] = d
    if severity:
        if event_type != "bark_episode":
            return None, _refuse("severity_only_for_bark_episode")
        if severity not in SEVERITIES:
            return None, _refuse("invalid_severity", allowed=SEVERITIES)
        when["severity_in"] = SEVERITIES[SEVERITIES.index(severity):]
    cd = 0
    if cooldown_s is not None:
        c = _num(cooldown_s)
        if c is None or not 0 <= c <= MAX_SECONDS:
            return None, _refuse("invalid_cooldown_s", min=0, max=MAX_SECONDS)
        cd = int(c)
    title, body = _TEXT[event_type]
    rule = {"id": f"ai_{event_type}_{secrets.token_hex(3)}", "enabled": True, "event_type": event_type,
            "when": when, "cooldown_s": cd, "attach": attach,
            "app_type": APP_TYPE_FOR_EVENT.get(event_type, "alert"),
            "title": title, "body": body, "override_quiet": False}
    return rule, None


async def set_notify_rule(client: RelayClient, dog_id: str, event_type: str, min_duration_s: Any = None,
                          severity: Optional[str] = None, attach: Optional[str] = None,
                          cooldown_s: Any = None) -> dict:
    rule, bad = build_notify_rule(event_type, min_duration_s, severity, attach, cooldown_s)
    if bad:
        return bad
    cur_row = await client.get_setting(dog_id, "notify_rules")
    rules = list((cur_row.get("value") or {}).get("rules", []))
    if len(rules) >= MAX_NOTIFY_RULES:
        return _refuse("notify_rules_limit", max=MAX_NOTIFY_RULES, rule_count=len(rules),
                       hint="The owner can remove a rule in the WIM-Z app.")
    row = await client.put_setting(dog_id, "notify_rules", {"rules": rules + [rule]},
                                   expected_version=cur_row.get("version"))
    saved = dict(next((r for r in row["value"].get("rules", []) if r.get("id") == rule["id"]), rule))
    saved["when"] = {k: v for k, v in (saved.get("when") or {}).items() if v is not None}
    return {"ok": True, "rule": saved, "rule_count": len(row["value"].get("rules", [])),
            "max_rules": MAX_NOTIFY_RULES, "version": row.get("version")}


# ---------------------------------------------------------------------------
# set_music (2026-11-18)
# ---------------------------------------------------------------------------

MUSIC_TRIGGER_MODELS = {"on_mode_start": MusicOnModeStart, "after_treat": MusicAfterTreat,
                        "on_bark_episode": MusicOnBarkEpisode, "at_time": MusicAtTime}
# fields that belong to one trigger only (track / duration_min apply to every trigger)
MUSIC_TRIGGER_FIELDS = {"on_mode_start": ("mode", "delay_s"), "after_treat": ("every_n", "minutes"),
                        "on_bark_episode": ("min_severity",), "at_time": ("time",)}
MAX_MUSIC_SLOTS = 12   # = MusicPlan.at_time max_length


async def set_music(client: RelayClient, dog_id: str, enabled: Optional[bool] = None, volume: Any = None,
                    trigger: Optional[str] = None, remove: bool = False, track: Optional[str] = None,
                    duration_min: Any = None, mode: Optional[str] = None, delay_s: Any = None,
                    every_n: Any = None, minutes: Any = None, min_severity: Optional[str] = None,
                    time: Optional[str] = None) -> dict:
    """Change the music plan. trigger + fields add or update that trigger (omitted fields keep their
    value, or the default for a new one); remove=True switches it off (at_time: the slot at `time`,
    or every slot without `time`). Everything not named keeps its value."""
    given = {k: v for k, v in {"mode": mode, "delay_s": delay_s, "every_n": every_n, "minutes": minutes,
                               "min_severity": min_severity, "time": time}.items() if v is not None}
    if trigger is not None and trigger not in MUSIC_TRIGGER_KINDS:
        return _refuse("invalid_trigger", allowed=list(MUSIC_TRIGGER_KINDS))
    if trigger is None and (given or track is not None or duration_min is not None or remove):
        return _refuse("trigger_required", allowed=list(MUSIC_TRIGGER_KINDS),
                       hint="name the trigger these fields belong to")
    if trigger is not None:
        stray = [k for k in given if k not in MUSIC_TRIGGER_FIELDS[trigger]]
        if stray:
            return _refuse("field_not_for_trigger", trigger=trigger, fields=stray,
                           allowed=["track", "duration_min", *MUSIC_TRIGGER_FIELDS[trigger]])
    if enabled is not None and not isinstance(enabled, bool):
        return _refuse("invalid_enabled", hint="true or false")
    vol = None
    if volume is not None:
        vol = _int_in(volume, 0, 100)
        if vol is None:
            return _refuse("invalid_volume", min=0, max=100, requested=volume)
    if enabled is None and volume is None and trigger is None:
        return _refuse("nothing_to_change", hint="pass enabled, volume or a trigger")

    cur_row = await client.get_setting(dog_id, "music_plan")
    plan = MusicPlan.model_validate(cur_row.get("value") or {}).model_dump()
    if enabled is not None:
        plan["enabled"] = enabled
    if vol is not None:
        plan["volume"] = vol

    if trigger is not None:
        if track is not None and not remove:
            sounds = (await client.get_setting(dog_id, "sounds")).get("value") or {}
            songs = [x.get("id") for x in sounds.get("sounds", []) if x.get("kind") == "song"]
            if track not in songs:
                return _refuse("unknown_track", requested=track, available_tracks=songs)
        fields = dict(given)
        if track is not None:
            fields["track"] = track
        if duration_min is not None:
            fields["duration_min"] = duration_min
        model = MUSIC_TRIGGER_MODELS[trigger]
        if trigger == "at_time":
            slots = list(plan["at_time"])
            if remove:
                plan["at_time"] = [x for x in slots if time is not None and x["time"] != time]
            else:
                if time is None or not _HM.match(str(time)):
                    return _refuse("invalid_time", hint="at_time needs time as HH:MM (24h, local)")
                old = next((x for x in slots if x["time"] == time), {})
                try:
                    slot = model.model_validate({**old, **fields}).model_dump()
                except ValidationError as e:
                    return _refuse("invalid_value", trigger=trigger, errors=_errors(e))
                slots = [x for x in slots if x["time"] != time] + [slot]
                if len(slots) > MAX_MUSIC_SLOTS:
                    return _refuse("too_many_music_slots", max=MAX_MUSIC_SLOTS)
                plan["at_time"] = sorted(slots, key=lambda x: x["time"])
        elif remove:
            plan[trigger] = None
        else:
            try:
                plan[trigger] = model.model_validate({**(plan.get(trigger) or {}), **fields}).model_dump()
            except ValidationError as e:
                return _refuse("invalid_value", trigger=trigger, errors=_errors(e))

    row = await client.put_setting(dog_id, "music_plan", plan, expected_version=cur_row.get("version"))
    v = row["value"]
    return {"ok": True, "music_enabled": v.get("enabled"), "volume": v.get("volume"), "music_plan": v,
            "version": row.get("version"),
            "note": "The robot runs these triggers itself; quiet hours still silence music."}


def _errors(e: ValidationError) -> list[dict]:
    return [{"field": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]} for err in e.errors()]
