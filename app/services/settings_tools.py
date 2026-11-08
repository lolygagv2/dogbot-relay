"""Treat-schedule windows and notification rules for connectors and the voice agent (2026-11-08).

Both tools sit in the `settings` scope and write through the relay's settings API as the
caller's internal actor, so the relay's own guards still apply on top of the checks here.

set_treat_schedule replaces `treat_schedule.windows` (and optionally default_multiplier).
daily_cap and max_per_hour are owner-only from these tools: they are copied unchanged from
the current value, never taken as input. The result reads back the saved windows.

set_notify_rule appends one rule to `notify_rules`, with the app_type that matches its event
type, up to MAX_NOTIFY_RULES rules per dog (the owner's existing rules count).
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from app.models import NotifyRule
from app.services.relay_client import RelayClient
from app.services.settings_service import minute_set

MAX_TREAT_WINDOWS = 12
MULTIPLIER_MIN, MULTIPLIER_MAX = 0.0, 3.0
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
    "departure": ("{dog_name} is home alone", "Guardian active"),
    "arrival": ("Someone is home", "{dog_name} has company"),
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


async def set_treat_schedule(client: RelayClient, dog_id: str, windows: Any,
                             default_multiplier: Optional[float] = None) -> dict:
    new_windows, bad = normalise_windows(windows)
    if bad:
        return bad
    if default_multiplier is not None:
        dm = _num(default_multiplier)
        if dm is None or not MULTIPLIER_MIN <= dm <= MULTIPLIER_MAX:
            return _refuse("invalid_multiplier", field="default_multiplier", min=MULTIPLIER_MIN, max=MULTIPLIER_MAX)
    cur_row = await client.get_setting(dog_id, "treat_schedule")
    cur = dict(cur_row.get("value") or {})
    new = dict(cur)                      # daily_cap / max_per_hour carried over untouched
    new["windows"] = new_windows
    if default_multiplier is not None:
        new["default_multiplier"] = dm
    row = await client.put_setting(dog_id, "treat_schedule", new, expected_version=cur_row.get("version"))
    v = row["value"]
    return {"ok": True, "windows": v.get("windows", []), "default_multiplier": v.get("default_multiplier"),
            "daily_cap": v.get("daily_cap"), "daily_cap_note": "unchanged; only the owner can change it in the app",
            "previous_windows": cur.get("windows", []), "version": row.get("version")}


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
