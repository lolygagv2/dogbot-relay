"""Shared per-dog settings with guards (Dec 9 demo build, 2026-11-06).

One JSON value per key in `dog_settings`. Read by the app, the MCP connector and the
voice agent through the same HTTP routes, so every guard lives here:

- treat daily cap (and hourly rate / closed windows) on relay-issued dispenses
- connectors/agents may only *widen* quiet hours, never narrow or disable them
- the treat daily cap and hourly limit can be changed by the app, the robot
  (treat_schedule is the one key a robot may write) and connectors/agents, within the
  TreatSchedule bounds (2026-11-09); an AI raising either one pushes an alert to the owner
- connectors/agents may not set `override_quiet` on notify rules, reset notify
  rules, or edit the sound list (reset to builtins is allowed)

Every write and every denial is recorded in `settings_audit`, and successful writes
are pushed to the owner's robots as `{"type": "settings_updated", ...}`.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.auth import Actor
from app.config import get_settings
from app.database import (
    count_events,
    count_pending_treat_ledger,
    delete_dog_setting,
    get_all_dog_setting_rows,
    get_dog_owner,
    get_dog_setting_row,
    get_user_paired_devices,
    insert_settings_audit,
    insert_treat_ledger,
    put_dog_setting,
)
from app.models import (
    Missions,
    MusicPlan,
    NotifyRules,
    QuietHours,
    SgSensitivity,
    Sounds,
    TreatSchedule,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keys, schemas, defaults
# ---------------------------------------------------------------------------

BUILTIN_SOUNDS = [
    {"id": "chime", "kind": "sound", "label": "Calm chime", "builtin": True, "file": "chime.wav"},
    {"id": "whistle", "kind": "sound", "label": "Recall whistle", "builtin": True, "file": "whistle.wav"},
    {"id": "shush", "kind": "sound", "label": "Soft shush", "builtin": True, "file": "shush.wav"},
    {"id": "good_dog", "kind": "sound", "label": "Good dog", "builtin": True, "file": "good_dog.wav"},
    # songs are sounds with kind=song; music_plan.track references these ids
    {"id": "song_calm_01", "kind": "song", "label": "Calm piano", "builtin": True, "file": "song_calm_01.mp3"},
    {"id": "song_calm_02", "kind": "song", "label": "Soft rain", "builtin": True, "file": "song_calm_02.mp3"},
]

DEFAULT_NOTIFY_RULES = {
    "rules": [
        {"id": "bark_long", "enabled": True, "event_type": "bark_episode",
         "when": {"min_duration_s": 60, "severity_in": ["med", "high"]},
         "cooldown_s": 900, "attach": "still", "app_type": "bark",
         "title": "{dog_name} is barking", "body": "{duration_min} min, {bark_count} barks"},
        {"id": "settled", "enabled": True, "event_type": "settle", "when": {},
         "cooldown_s": 600, "attach": "none", "app_type": "alert",
         "title": "{dog_name} settled", "body": "after {settle_min} min"},
        {"id": "departure", "enabled": True, "event_type": "departure", "when": {},
         "cooldown_s": 1800, "attach": "none", "app_type": "alert",
         "title": "Guardian active", "body": "{dog_name} is home alone"},
        {"id": "mission_fail", "enabled": True, "event_type": "mission_run",
         "when": {"result_in": ["fail"]}, "cooldown_s": 0, "attach": "clip",
         "app_type": "missionFailed", "title": "Mission failed", "body": "{mission_type}"},
        {"id": "treat_remote", "enabled": True, "event_type": "treat",
         "when": {"source_in": ["connector", "agent"]}, "cooldown_s": 0, "attach": "still",
         "app_type": "treatDispensed", "title": "Treat given", "body": "by {source}"},
    ]
}


def _default_sounds(dog_id: str) -> dict:
    return {"sounds": [dict(s, url=sound_url(dog_id, s["id"])) for s in BUILTIN_SOUNDS]}


def sound_url(dog_id: str, sound_id: str) -> str:
    return f"/api/sounds/file/{dog_id}/{sound_id}"


SETTING_MODELS: dict[str, Optional[type[BaseModel]]] = {
    "timezone": None,  # plain IANA string
    "quiet_hours": QuietHours,
    "treat_schedule": TreatSchedule,
    "sg_sensitivity": SgSensitivity,
    "music_plan": MusicPlan,
    "notify_rules": NotifyRules,
    "missions": Missions,
    "sounds": Sounds,
}

SETTING_KEYS = tuple(SETTING_MODELS.keys())


def default_value(dog_id: str, key: str) -> Any:
    if key == "timezone":
        return get_settings().default_timezone
    if key == "notify_rules":
        return NotifyRules.model_validate(DEFAULT_NOTIFY_RULES).model_dump()
    if key == "sounds":
        return Sounds.model_validate(_default_sounds(dog_id)).model_dump()
    model = SETTING_MODELS[key]
    return model().model_dump()


def validate_value(key: str, value: Any) -> Any:
    """Normalise + validate a settings value; raises HTTP 422 on schema errors."""
    if key not in SETTING_MODELS:
        raise HTTPException(status_code=404, detail={"code": "unknown_setting", "key": key})
    if key == "timezone":
        if not isinstance(value, str) or not value:
            raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": "timezone must be an IANA string"})
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": f"unknown timezone {value!r}"})
        return value
    model = SETTING_MODELS[key]
    try:
        parsed = model.model_validate(value)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "errors": e.errors(include_url=False)})
    if key == "quiet_hours":
        _hm_to_minutes(parsed.start)
        _hm_to_minutes(parsed.end)
    if key == "treat_schedule":
        for w in parsed.windows:
            _hm_to_minutes(w.start)
            _hm_to_minutes(w.end)
    if key == "notify_rules":
        ids = [r.id for r in parsed.rules]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": "duplicate rule id"})
    if key == "missions":
        ids = [m.id for m in parsed.missions]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": "duplicate mission id"})
    return parsed.model_dump()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_setting(dog_id: str, key: str) -> dict:
    if key not in SETTING_MODELS:
        raise HTTPException(status_code=404, detail={"code": "unknown_setting", "key": key})
    row = get_dog_setting_row(dog_id, key)
    if row:
        return row
    return {"key": key, "value": default_value(dog_id, key), "version": 0,
            "updated_at": None, "updated_by": "default"}


def get_value(dog_id: str, key: str) -> Any:
    return get_setting(dog_id, key)["value"]


def get_all_settings(dog_id: str) -> dict[str, dict]:
    rows = get_all_dog_setting_rows(dog_id)
    out = {}
    for key in SETTING_KEYS:
        out[key] = rows.get(key) or {"key": key, "value": default_value(dog_id, key), "version": 0,
                                      "updated_at": None, "updated_by": "default"}
    return out


def get_timezone(dog_id: str) -> ZoneInfo:
    name = get_value(dog_id, "timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(get_settings().default_timezone)


def local_now(dog_id: str) -> datetime:
    return datetime.now(timezone.utc).astimezone(get_timezone(dog_id))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _hm_to_minutes(hm: str) -> int:
    try:
        h, m = hm.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": f"bad time {hm!r}"})
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": f"bad time {hm!r}"})
    return h * 60 + m


def minute_set(start: str, end: str) -> set[int]:
    """Minutes-of-day covered by [start, end); wraps past midnight when end <= start."""
    s, e = _hm_to_minutes(start), _hm_to_minutes(end)
    if s == e:
        return set(range(0, 1440))
    if s < e:
        return set(range(s, e))
    return set(range(s, 1440)) | set(range(0, e))


def in_window(local_dt: datetime, start: str, end: str) -> bool:
    return (local_dt.hour * 60 + local_dt.minute) in minute_set(start, end)


def in_quiet_hours(dog_id: str, local_dt: Optional[datetime] = None) -> bool:
    qh = QuietHours.model_validate(get_value(dog_id, "quiet_hours"))
    if not qh.enabled:
        return False
    local_dt = local_dt or local_now(dog_id)
    return in_window(local_dt, qh.start, qh.end)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

class GuardDenied(Exception):
    def __init__(self, guard: str, reason: str):
        super().__init__(reason)
        self.guard = guard
        self.reason = reason


def _guard_put(actor: Actor, key: str, old: Any, new: Any) -> None:
    """Raise GuardDenied if this actor may not make this change. App/admin are unrestricted."""
    if not actor.is_internal:
        return
    if key == "quiet_hours":
        o, n = QuietHours.model_validate(old), QuietHours.model_validate(new)
        # Connectors/agents may turn quiet hours ON (any window) or widen an enabled window;
        # never turn them off or narrow them. A write with enabled=false is never theirs to make.
        if not n.enabled:
            raise GuardDenied("quiet_hours_widen_only", "connector cannot disable quiet hours")
        if o.enabled and not minute_set(o.start, o.end) <= minute_set(n.start, n.end):
            raise GuardDenied("quiet_hours_widen_only",
                              f"connector may only widen quiet hours (current {o.start}-{o.end})")
    elif key == "notify_rules":
        n = NotifyRules.model_validate(new)
        if any(r.override_quiet for r in n.rules):
            raise GuardDenied("notify_no_override_quiet", "connector cannot set override_quiet on a rule")
    elif key == "sounds":
        raise GuardDenied("sounds_app_only", "connector cannot edit the sound list (reset is allowed)")
    elif key == "timezone":
        raise GuardDenied("timezone_app_only", "connector cannot change the dog's timezone")


def _guard_reset(actor: Actor, key: str) -> None:
    if actor.is_internal and key in ("notify_rules", "timezone"):
        raise GuardDenied(f"{key}_reset_app_only", f"connector cannot reset {key}")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def put_setting(dog_id: str, key: str, value: Any, actor: Actor,
                      expected_version: Optional[int] = None) -> dict:
    new_value = validate_value(key, value)
    current = get_setting(dog_id, key)
    try:
        _guard_put(actor, key, current["value"], new_value)
    except GuardDenied as g:
        insert_settings_audit(dog_id, key, actor.label, "denied", current["value"], new_value, g.reason)
        raise HTTPException(status_code=403, detail={"code": "guard_denied", "guard": g.guard, "reason": g.reason})
    row = put_dog_setting(dog_id, key, new_value, actor.label, expected_version)
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "version_mismatch", "current_version": current["version"]})
    insert_settings_audit(dog_id, key, actor.label, "put", current["value"], new_value, None)
    await push_settings_to_robot(dog_id, key, row)
    if key == "treat_schedule" and actor.is_internal:
        await _alert_owner_if_treat_limits_raised(dog_id, actor, current["value"], row["value"])
    if key == "missions":
        # amendment 2026-11-06 (3): a new/changed departure mission starts within 15 min
        # even if no departure event arrives
        try:
            from app.services import notify_service
            notify_service.arm_departure_fallbacks(dog_id, current["value"], new_value, actor.user_id)
        except Exception as e:  # pragma: no cover
            logger.warning(f"[SETTINGS] arming departure fallbacks failed: {e}")
    return row


async def remove_mission_deleted_on_robot(owner_id: Optional[str], device_id: str, mission_id: str,
                                          dog_id: Optional[str] = None) -> Optional[str]:
    """The owner deleted a relay-made schedule in the app, which deletes it on the robot only.
    The robot's `schedule_deleted` names the mission; drop it from the dog's `missions` setting
    so the next push or reconnect doesn't bring it back. Only the owner's own dogs are touched.
    Returns the dog_id it was removed from, or None. Never raises."""
    try:
        if not owner_id or not mission_id:
            return None
        from app.database import get_user_dogs
        owned = [d["id"] for d in get_user_dogs(owner_id) if d.get("role") == "owner"]
        for d in ([dog_id] if dog_id in owned else owned):
            missions = (get_value(d, "missions") or {}).get("missions", [])
            keep = [m for m in missions if m.get("id") != mission_id]
            if len(keep) == len(missions):
                continue
            actor = Actor(kind="robot", id=device_id, user_id=owner_id, device_id=device_id)
            await put_setting(d, "missions", {"missions": keep}, actor)
            logger.info(f"[SCHEDULE] {device_id} deleted {mission_id}: removed from {d} missions")
            return d
        return None
    except Exception as e:
        logger.warning(f"[SCHEDULE] could not remove {mission_id} after robot delete: {e}")
        return None


async def reset_setting(dog_id: str, key: str, actor: Actor) -> dict:
    if key not in SETTING_MODELS:
        raise HTTPException(status_code=404, detail={"code": "unknown_setting", "key": key})
    current = get_setting(dog_id, key)
    try:
        _guard_reset(actor, key)
    except GuardDenied as g:
        insert_settings_audit(dog_id, key, actor.label, "denied", current["value"], None, g.reason)
        raise HTTPException(status_code=403, detail={"code": "guard_denied", "guard": g.guard, "reason": g.reason})
    default = default_value(dog_id, key)
    row = put_dog_setting(dog_id, key, default, actor.label)
    insert_settings_audit(dog_id, key, actor.label, "reset", current["value"], default, None)
    await push_settings_to_robot(dog_id, key, row)
    return row


def delete_all_settings(dog_id: str) -> None:
    for key in SETTING_KEYS:
        delete_dog_setting(dog_id, key)


async def push_settings_to_robot(dog_id: str, key: str, row: dict) -> int:
    """Send settings_updated to every robot paired with the dog's owner. Never raises."""
    try:
        from app.connection_manager import get_connection_manager
        owner = get_dog_owner(dog_id)
        if not owner:
            return 0
        manager = get_connection_manager()
        msg = {
            "type": "settings_updated",
            "dog_id": dog_id,
            "key": key,
            "value": row["value"],
            "version": row["version"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        sent = 0
        for device_id in get_user_paired_devices(owner):
            if manager.is_robot_online(device_id) and await manager.send_to_robot(device_id, msg):
                sent += 1
        return sent
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[SETTINGS] push_settings_to_robot failed for {dog_id}/{key}: {e}")
        return 0


# ---------------------------------------------------------------------------
# Treat cap (relay-issued dispenses)
# ---------------------------------------------------------------------------

def treat_limits_raised(old: Any, new: Any) -> list[str]:
    """Human phrases for each treat limit that went UP between two treat_schedule values."""
    o, n = TreatSchedule.model_validate(old or {}), TreatSchedule.model_validate(new or {})
    out = []
    if n.daily_cap > o.daily_cap:
        out.append(f"daily treat cap {o.daily_cap} → {n.daily_cap}")
    if n.max_per_hour > o.max_per_hour:
        out.append(f"hourly treat limit {o.max_per_hour} → {n.max_per_hour}")
    return out


async def _alert_owner_if_treat_limits_raised(dog_id: str, actor: Actor, old: Any, new: Any) -> str:
    """Push the owner a heads-up when an assistant (connector/agent) raises a treat limit,
    so a misheard or mistaken change is visible and can be undone in the app. Never raises."""
    try:
        raised = treat_limits_raised(old, new)
        if not raised:
            return "not_raised"
        owner = get_dog_owner(dog_id)
        if not owner:
            return "no_owner"
        from app.database import get_dog_by_id
        from app.services import push_service
        dog = get_dog_by_id(dog_id) or {}
        name = dog.get("name") or "your dog"
        who = "The voice assistant" if actor.kind == "agent" else "A connected AI assistant"
        body = f"{who} raised {name}'s {' and '.join(raised)}. Open WIM-Z to change it back."
        outcome = await push_service.send_push(
            user_id=owner, app_type="alert", title="Treat limit raised", body=body,
            data={"type": "alert", "dog_id": dog_id, "kind": "treat_limit_raised", "actor": actor.label},
        )
        logger.info(f"[SETTINGS] treat limit raised by {actor.label} for {dog_id}: {raised} (push={outcome})")
        return outcome
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[SETTINGS] treat-limit alert failed for {dog_id}: {e}")
        return "error"


# Field names an app `sg_config` message may carry the treat limits under. The relay
# does not own that payload's schema, so it accepts any of these (top level or nested
# one level under data/payload/config/settings/treats).
_APP_DAILY_CAP_KEYS = ("daily_cap", "treat_daily_cap", "daily_treat_cap", "daily_treat_limit", "treat_limit",
                       "treat_cap", "max_treats_per_day", "max_daily_treats", "treats_per_day",
                       "max_treats", "treat_limit_per_day", "daily_treats_max")
_APP_HOURLY_KEYS = ("max_per_hour", "treat_max_per_hour", "max_treats_per_hour", "treats_per_hour",
                    "hourly_treat_limit", "treat_hourly_limit")
_NEST_KEYS = ("data", "payload", "config", "settings", "treats", "treat", "treat_schedule")


def _find_int(msg: dict, keys: tuple[str, ...]) -> Optional[int]:
    layers = [msg] + [msg[k] for k in _NEST_KEYS if isinstance(msg.get(k), dict)]
    for layer in list(layers):
        layers += [layer[k] for k in _NEST_KEYS if isinstance(layer.get(k), dict) and layer[k] not in layers]
    for layer in layers:
        for k in keys:
            v = layer.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
                return int(float(v))
    return None


def _payload_shape(msg: Any, depth: int = 0) -> Any:
    """Keys (and nested keys) of a message, without values - safe to log."""
    if isinstance(msg, dict) and depth < 3:
        return {k: _payload_shape(v, depth + 1) for k, v in msg.items()}
    return type(msg).__name__


async def sync_treat_limits_from_app_message(user_id: str, device_id: Optional[str], message: dict) -> Optional[dict]:
    """The app saves its treat limit by sending `sg_config` straight to the robot. Mirror any
    daily cap / hourly limit found in it into the dog's relay `treat_schedule` (as the app
    user), so the relay - which the robot and the AI assistants read - stays the one source
    of truth. Writes only when a value actually changes. Never raises; returns the new row."""
    try:
        logger.info(f"[TREAT-SYNC] {message.get('type') or message.get('command')} from {user_id} "
                    f"shape={_payload_shape(message)}")
        cap = _find_int(message, _APP_DAILY_CAP_KEYS)
        hourly = _find_int(message, _APP_HOURLY_KEYS)
        if cap is None and hourly is None:
            return None
        sub = message.get("data") if isinstance(message.get("data"), dict) else {}
        dog_ids = [message.get("dog_id") or sub.get("dog_id")] if (message.get("dog_id") or sub.get("dog_id")) else []
        if not dog_ids and device_id:
            from app.database import get_device_dog_ids
            dog_ids = get_device_dog_ids(device_id)
        if len(dog_ids) != 1:
            logger.warning(f"[TREAT-SYNC] cap={cap} hourly={hourly} but dog is ambiguous for {device_id}: {dog_ids}")
            return None
        dog_id = dog_ids[0]
        from app.auth import check_dog_access
        actor = Actor(kind="app", id=user_id, user_id=user_id)
        check_dog_access(actor, dog_id, write=True)
        cur = get_setting(dog_id, "treat_schedule")
        new = dict(cur["value"])
        if cap is not None:
            new["daily_cap"] = cap
        if hourly is not None:
            new["max_per_hour"] = hourly
        if new == cur["value"]:
            return None
        row = await put_setting(dog_id, "treat_schedule", new, actor)
        logger.info(f"[TREAT-SYNC] {dog_id} treat_schedule daily_cap={row['value']['daily_cap']} "
                    f"max_per_hour={row['value']['max_per_hour']} (v{row['version']}) from app sg_config")
        return row
    except Exception as e:
        logger.warning(f"[TREAT-SYNC] failed for {user_id}/{device_id}: {e}")
        return None

def treats_used_today(dog_id: str) -> tuple[int, str]:
    """(count, local_date) of treats today: robot treat events + in-flight ledger rows."""
    now_local = local_now(dog_id)
    today = now_local.date().isoformat()
    used = count_events(dog_id, "treat", local_date=today)
    used += count_pending_treat_ledger(dog_id, today, within_seconds=120)
    return used, today


def _next_local_midnight_utc(now_local: datetime) -> str:
    nxt = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc).isoformat()


def check_treat_cap(dog_id: str, actor: Actor, n: int = 1) -> dict:
    """Enforce daily cap, hourly rate and windows; reserve a ledger row on success.

    Raises HTTP 429 (cap / rate) or 403 (window closed). Returns the guard summary,
    including the reserved `command_id` the robot should echo on its treat event.
    """
    schedule = TreatSchedule.model_validate(get_value(dog_id, "treat_schedule"))
    now_local = local_now(dog_id)
    used, today = treats_used_today(dog_id)

    if used + n > schedule.daily_cap:
        raise HTTPException(status_code=429, detail={
            "code": "treat_cap_reached", "used": used, "cap": schedule.daily_cap,
            "resets_at": _next_local_midnight_utc(now_local),
        })

    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    hour_used = count_events(dog_id, "treat", since_ts=hour_ago)
    if schedule.max_per_hour and hour_used + n > schedule.max_per_hour:
        raise HTTPException(status_code=429, detail={
            "code": "treat_rate", "used_last_hour": hour_used, "max_per_hour": schedule.max_per_hour,
        })

    multiplier = schedule.default_multiplier
    for w in schedule.windows:
        if in_window(now_local, w.start, w.end):
            multiplier = w.multiplier
            break
    if multiplier == 0:
        raise HTTPException(status_code=403, detail={"code": "treat_window_closed"})

    command_id = str(uuid.uuid4())
    insert_treat_ledger(command_id, dog_id, actor.label, today)
    return {
        "treats_used": used + n,
        "treat_cap": schedule.daily_cap,
        "multiplier": multiplier,
        "command_id": command_id,
    }
