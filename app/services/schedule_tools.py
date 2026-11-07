"""Mission scheduling for connectors and the voice agent (2026-11-07).

There is no draft/approval step: schedule_mission writes the mission into the dog's `missions`
setting (which the relay pushes to the robot as `settings_updated`), and cancel_mission removes it.
Both then ask the robot what it actually has loaded with `schedule_sync`, so the result says
whether the robot confirmed it and when it next fires.

Robot contract (schedule_sync): the relay sends
  {"type": "command", "command": "schedule_sync", "command_id": ..., "data": {"dog_id": ...}}
and the robot answers with the usual command_result:
  {"type": "command_result", "command_id": ..., "ok": true,
   "result": {"missions": [{"id": "m_...", "enabled": true, "next_fire_at": "2026-11-08T18:00:00-05:00"}],
              "count": 1, "scheduler": {"enabled": true, "now": "...", "check_interval_s": 5.0}, "active": null}}
(Shape confirmed against wimz_robot_05 on 2026-11-07.)
next_fire_at is null for a mission that will not fire (disabled, or not a schedule trigger).

Mission types: training missions (recall, sit_stay, ...) run up to 60 minutes; mode missions
(silent_guardian, coach) put the robot in that mode for duration_s (up to 12 hours), then idle.
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from app.models import MODE_MAX_DURATION_S, MODE_MISSION_TYPES, TRAINING_MAX_DURATION_S
from app.services.relay_client import RelayClient, RelayRefusal

TRAINING_TYPES = ["recall", "sit_stay", "down_stay", "place", "quiet", "find_treat", "sit", "down", "stay", "spin"]
MODE_TYPES = list(MODE_MISSION_TYPES)
ALL_TYPES = TRAINING_TYPES + MODE_TYPES
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TRAINING_MAX_MIN = TRAINING_MAX_DURATION_S // 60   # 60
MODE_MAX_MIN = MODE_MAX_DURATION_S // 60           # 720
MODE_MIN_MIN = 5
SYNC_WAIT_S = 8
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TYPE_LABEL = {"silent_guardian": "Silent Guardian", "coach": "Coach mode"}


def type_label(t: str) -> str:
    return _TYPE_LABEL.get(t, t.replace("_", " "))


def _refused(reason: str, **extra: Any) -> dict:
    return {"ok": False, "refused": True, "reason": reason, **extra}


def _str_or_none(v: Any, n: int = 64) -> Optional[str]:
    return v[:n] if isinstance(v, str) and v else None


async def robot_schedule(client: RelayClient, dog_id: str) -> dict:
    """Ask the robot what it has loaded. {confirmed: True, missions: {id: {...}}} or {confirmed: False, reason}."""
    try:
        res = await client.command(dog_id, "schedule_sync", {}, wait_s=SYNC_WAIT_S)
    except RelayRefusal as e:
        return {"confirmed": False, "reason": e.code}
    if res.get("simulated"):
        return {"confirmed": False, "reason": "robot_offline_demo"}
    r = res.get("result")
    if res.get("timed_out") or not isinstance(r, dict):
        return {"confirmed": False, "reason": "robot_did_not_confirm"}
    if not r.get("ok", True):
        return {"confirmed": False, "reason": "robot_error", "error": str(r.get("error") or "")[:120]}
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    raw = inner.get("missions")
    if not isinstance(raw, list):
        return {"confirmed": False, "reason": "robot_reply_malformed"}
    out: dict[str, dict] = {}
    for m in raw[:100]:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            entry = {"enabled": bool(m.get("enabled", True)), "next_fire_at": _str_or_none(m.get("next_fire_at"))}
            # e.g. find_treat / spin: kept by the robot but next_fire_at null with a reason
            reason = next((m[k] for k in ("reason", "unsupported_reason", "status") if _str_or_none(m.get(k))), None)
            if reason:
                entry["reason"] = reason[:120]
            out[m["id"][:64]] = entry
    sched = inner.get("scheduler") if isinstance(inner.get("scheduler"), dict) else {}
    active = inner.get("active")
    extra: dict[str, Any] = {}
    if isinstance(sched.get("enabled"), bool):
        extra["scheduler_enabled"] = sched["enabled"]
    if _str_or_none(sched.get("now")):
        extra["robot_time"] = sched["now"][:40]
    if isinstance(active, dict):   # the mission running right now, if any
        extra["active"] = {k: active[k] for k in ("id", "type", "name", "started_at", "ends_at")
                           if isinstance(active.get(k), str)}
    elif isinstance(active, str) and active:
        extra["active"] = {"id": active[:64]}
    return {"confirmed": True, "missions": out, **extra}


def _summary(m: dict) -> dict:
    trig = m.get("trigger") or {}
    return {"id": m.get("id"), "name": m.get("name"), "type": m.get("type"), "enabled": m.get("enabled", True),
            "trigger": trig.get("kind"), "time": trig.get("time"), "days": trig.get("days"),
            "duration_min": round(int(m.get("duration_s") or 0) / 60, 1)}


async def _missions(client: RelayClient, dog_id: str) -> tuple[list[dict], Optional[int]]:
    row = await client.get_setting(dog_id, "missions")
    return list(((row.get("value") or {}).get("missions")) or []), row.get("version")


def _attach(summary: dict, robot: dict) -> dict:
    if robot.get("confirmed"):
        r = robot["missions"].get(summary["id"])
        summary["on_robot"] = r is not None
        summary["next_fire_at"] = r["next_fire_at"] if r else None
        if r and r.get("reason"):
            summary["robot_note"] = r["reason"]
    return summary


def _robot_fields(robot: dict) -> dict:
    out: dict[str, Any] = {"confirmed_by_robot": bool(robot.get("confirmed"))}
    if not robot.get("confirmed"):
        out["robot_reason"] = robot.get("reason")
    for k in ("scheduler_enabled", "robot_time", "active"):
        if k in robot:
            out[k] = robot[k]
    if robot.get("scheduler_enabled") is False:
        out["warning"] = "The robot's scheduler is turned off, so scheduled missions will not fire."
    return out


async def get_schedule(client: RelayClient, dog_id: str) -> dict:
    try:
        missions, _ = await _missions(client, dog_id)
    except RelayRefusal as e:
        return e.as_result(_status=e.status)
    robot = await robot_schedule(client, dog_id)
    out = {"ok": True, **_robot_fields(robot), "missions": [_attach(_summary(m), robot) for m in missions]}
    if robot.get("confirmed"):
        known = {m.get("id") for m in missions}
        extra = [i for i in robot["missions"] if i not in known]
        if extra:
            out["robot_only_mission_ids"] = extra
    else:
        out["note"] = "This is the relay's copy; the robot did not confirm what it has loaded."
    return out


async def _put_missions(client: RelayClient, dog_id: str, change) -> dict:
    """Read-modify-write the missions setting, retrying once if another writer got in first."""
    for attempt in (1, 2):
        missions, version = await _missions(client, dog_id)
        new = change(missions)
        if isinstance(new, dict):   # a refusal from `change`
            return new
        try:
            return await client.put_setting(dog_id, "missions", {"missions": new}, expected_version=version)
        except RelayRefusal as e:
            if e.code != "version_mismatch" or attempt == 2:
                raise
    raise AssertionError("unreachable")


async def schedule_mission(client: RelayClient, dog_id: str, type: str, time: str, days: Optional[list[str]] = None,
                           duration_min: Optional[int] = None, name: Optional[str] = None,
                           reward_treats: Optional[int] = None, goal: str = "") -> dict:
    if type not in ALL_TYPES:
        return _refused("unknown_mission_type", allowed=ALL_TYPES)
    if not isinstance(time, str) or not _HM.match(time):
        return _refused("time_must_be_HH:MM_24h")
    days = [d for d in (days or []) if d in DAYS] or list(DAYS)
    is_mode = type in MODE_TYPES
    lo, hi, default = (MODE_MIN_MIN, MODE_MAX_MIN, 120) if is_mode else (1, TRAINING_MAX_MIN, 2)
    minutes = default if duration_min in (None, -1, 0) else int(duration_min)
    if not lo <= minutes <= hi:
        return _refused("duration_out_of_range", min_minutes=lo, max_minutes=hi)
    treats = 0 if is_mode else max(0, min(3, 1 if reward_treats in (None, -1) else int(reward_treats)))
    tl = type_label(type)
    label = (name or "").strip() or f"{tl[0].upper()}{tl[1:]} at {time}"
    mission = {
        "id": f"m_{type}_{time.replace(':', '')}_{secrets.token_hex(2)}",
        "type": type, "name": label[:120], "enabled": True,
        "trigger": {"kind": "schedule", "time": time, "delay_s": 0, "cooldown_s": 3600, "days": days},
        "duration_s": max(10, minutes * 60), "reward_treats": treats, "goal": (goal or "")[:240],
    }
    warnings: list[str] = []

    def add(missions: list[dict]):
        for m in missions:
            t = m.get("trigger") or {}
            if t.get("kind") == "schedule" and t.get("time") == time and m.get("enabled", True):
                warnings.append(f"{m.get('name') or m.get('id')} is also scheduled at {time}.")
        return missions + [mission]

    try:
        await _put_missions(client, dog_id, add)
    except RelayRefusal as e:
        return e.as_result(_status=e.status)
    robot = await robot_schedule(client, dog_id)
    out = {"ok": True, "scheduled": True, "mission_id": mission["id"], **_summary(mission), **_robot_fields(robot),
           "warnings": warnings}
    out.pop("id", None)
    if robot.get("confirmed"):
        r = robot["missions"].get(mission["id"])
        if r is None:
            out.update(confirmed_by_robot=False, robot_reason="robot_has_not_loaded_mission")
        else:
            out["next_fire_at"] = r["next_fire_at"]
            if r.get("reason"):
                out["robot_note"] = r["reason"]
    if out.get("next_fire_at"):
        out["message"] = f"Scheduled on the robot; next run {out['next_fire_at']}."
    elif out["confirmed_by_robot"]:
        out["message"] = f"The robot has it but will not run it ({out.get('robot_note') or 'no next run'})."
    else:
        out["message"] = f"Saved, but the robot has not confirmed it ({out.get('robot_reason')})."
    return out


async def cancel_mission(client: RelayClient, dog_id: str, mission_id: str) -> dict:
    found: dict = {}

    def remove(missions: list[dict]):
        keep = [m for m in missions if m.get("id") != mission_id]
        if len(keep) == len(missions):
            return _refused("unknown_mission", mission_id=mission_id,
                            known_mission_ids=[m.get("id") for m in missions][:30])
        found.update(next(m for m in missions if m.get("id") == mission_id))
        return keep

    try:
        res = await _put_missions(client, dog_id, remove)
    except RelayRefusal as e:
        return e.as_result(_status=e.status)
    if res.get("refused"):
        return res
    robot = await robot_schedule(client, dog_id)
    out = {"ok": True, "cancelled": True, "mission_id": mission_id, "name": found.get("name"),
           "type": found.get("type"), **_robot_fields(robot)}
    if robot.get("confirmed") and mission_id in robot["missions"]:
        out.update(confirmed_by_robot=False, robot_reason="robot_still_has_mission")
    out["message"] = ("Removed, and the robot confirmed it is gone." if out["confirmed_by_robot"]
                      else f"Removed on the relay, but the robot has not confirmed it ({out['robot_reason']}).")
    return out
