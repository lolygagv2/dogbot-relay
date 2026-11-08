"""Per-dog event store, ingest pipeline and daily roll-up (Dec 9 demo build, 2026-11-06).

Robots post typed events to `POST /events`; the replayer writes the same table with
`source='replay'`; relay-simulated commands write `source='relay'`. Every accepted event
flows through the same pipeline: validate -> insert (idempotent on event_id) -> link
treat ledger -> mark the day's summary dirty -> mirror into `activity_events` (so the
existing app timeline keeps working) -> notify rules -> departure triggers -> agent
hooks -> WebSocket forward to the owner's app sessions.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.database import (
    daily_rows_fresh_for_week,
    get_daily_summary_row,
    get_weekly_summary_row,
    get_dog_owner,
    get_event,
    get_user_dogs,
    get_user_paired_devices,
    insert_activity_event,
    insert_event,
    last_device_event_before,
    link_treat_ledger,
    list_device_events,
    list_events,
    mark_summaries_dirty_from,
    mark_summary_dirty,
    upsert_daily_summary,
    upsert_weekly_summary,
)
from app.config import get_settings
from app.models import EVENT_TYPES, NotifyRules, TreatSchedule
from app.services import settings_service

logger = logging.getLogger(__name__)

MEDIA_PREFIX = "/api/media/download/"
SETTLE_WINDOW_S = 600  # a settle within episode end + 10 min counts for that episode
LOW_HOPPER_THRESHOLD = 3
TRICK_RESULTS = ("success", "fail", "no_response")
# mode time (2026-11-07): totals.<key> = seconds the robot spent in that mode on the local day
MODE_TOTALS = {"silent_guardian": "sg_s", "coach": "coach_s"}
HEARTBEAT_GRACE_S = 120  # a mode period ends this long after the robot's last status heartbeat

# last known robot mode per device (for sg_rearm detection); in-process only
_last_mode: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def normalize_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 (naive = UTC) or epoch seconds/ms -> aware UTC datetime; None if unparsable."""
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e12:  # epoch ms
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(value, str):
            s = value.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _clean_media(data: dict) -> dict:
    for key in ("image_url", "clip_url"):
        v = data.get(key)
        if v is not None and not (isinstance(v, str) and v.startswith(MEDIA_PREFIX) and len(v) < 512):
            data.pop(key, None)
    return data


def validate_event(ev: dict) -> tuple[Optional[dict], Optional[str]]:
    """Return (normalised_event, None) or (None, reason)."""
    etype = ev.get("type")
    if etype not in EVENT_TYPES:
        return None, f"unknown type {etype!r}"
    event_id = str(ev.get("event_id") or "").strip()
    if not event_id or len(event_id) > 128:
        return None, "event_id required"
    dog_id = str(ev.get("dog_id") or "").strip() or None
    if dog_id is None and etype not in ("status", "mode_change"):
        # status / mode_change are device-scoped (contract fix 2026-11-06); resolved on ingest
        return None, "dog_id required"
    dt = normalize_ts(ev.get("ts"))
    if dt is None:
        return None, "ts unparsable"
    duration = ev.get("duration_s")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            return None, "duration_s must be a number"
        if duration < 0 or duration > 86400:
            return None, "duration_s out of range"
    data = ev.get("data")
    data = dict(data) if isinstance(data, dict) else {}
    data = _clean_media(data)

    if etype == "bark_episode":
        if duration is None:
            return None, "bark_episode requires duration_s"
        if not isinstance(data.get("bark_count"), (int, float)):
            return None, "bark_episode requires data.bark_count"
        if data.get("severity") not in ("low", "med", "high"):
            return None, "bark_episode requires data.severity in low|med|high"
    elif etype == "intervention":
        if data.get("kind") not in ("sound", "voice", "treat", "music", "approach"):
            return None, "intervention requires data.kind in sound|voice|treat|music|approach"
    elif etype == "pacing":
        if duration is None:
            return None, "pacing requires duration_s"
    elif etype == "treat":
        if data.get("source") not in ("auto", "app", "connector", "agent", "schedule", "replay", "relay"):
            return None, "treat requires data.source"
        if "count" in data:
            try:
                data["count"] = max(1, int(data["count"]))
            except (TypeError, ValueError):
                return None, "treat data.count must be an integer"
    elif etype == "music":
        if data.get("action") not in ("start", "stop"):
            return None, "music requires data.action start|stop"
        if not data.get("track"):
            return None, "music requires data.track"
    elif etype == "mission_run":
        if not (data.get("mission_id") or data.get("mission_type")):
            return None, "mission_run requires data.mission_id or data.mission_type"
        if data.get("result") not in ("success", "fail", "aborted"):
            return None, "mission_run requires data.result in success|fail|aborted"
    elif etype == "coach_session":
        # one Coach session for one dog: ts = start, duration_s = length. data.cues /
        # data.successes are the robot's own tally (informational; roll-ups count trick_attempt).
        if duration is None:
            return None, "coach_session requires duration_s"
        for k in ("cues", "successes"):
            if k in data:
                try:
                    data[k] = max(0, int(data[k]))
                except (TypeError, ValueError):
                    return None, f"coach_session data.{k} must be an integer"
    elif etype == "trick_attempt":
        # one cue: data.trick (e.g. "sit") and the outcome, as data.result success|fail|no_response
        # or data.success (bool). Optional: latency_ms, session_id (the coach_session event_id), treat.
        trick = data.get("trick")
        if not isinstance(trick, str) or not trick.strip() or len(trick) > 64:
            return None, "trick_attempt requires data.trick"
        data["trick"] = trick.strip().lower()
        result = data.get("result")
        if result is None and isinstance(data.get("success"), bool):
            result = "success" if data["success"] else "fail"
        if result not in TRICK_RESULTS:
            return None, "trick_attempt requires data.result in success|fail|no_response (or data.success)"
        data["result"], data["success"] = result, result == "success"
        if "latency_ms" in data:
            try:
                data["latency_ms"] = max(0, int(data["latency_ms"]))
            except (TypeError, ValueError):
                data.pop("latency_ms", None)
    elif etype == "mode_change":
        mode = data.get("mode")
        if not isinstance(mode, str) or not mode.strip() or len(mode) > 32:
            return None, "mode_change requires data.mode"
        data["mode"] = mode.strip().lower()
    elif etype == "status":
        if not isinstance(data.get("battery"), (int, float)):
            return None, "status requires data.battery"
        if not data.get("mode"):
            return None, "status requires data.mode"
        # amendment 2026-11-06 (6): docked (bool) and light_level (0-255) ride along; never rolled up
        if "docked" in data:
            data["docked"] = bool(data["docked"])
        if "light_level" in data:
            try:
                data["light_level"] = max(0, min(255, int(data["light_level"])))
            except (TypeError, ValueError):
                data.pop("light_level", None)

    return {
        "event_id": event_id,
        "type": etype,
        "dog_id": dog_id,
        "ts": dt.isoformat(),
        "_dt": dt,
        "duration_s": duration,
        "data": data,
    }, None


def local_date_for(dt_utc: datetime, tz: ZoneInfo) -> str:
    return dt_utc.astimezone(tz).date().isoformat()


# ---------------------------------------------------------------------------
# Activity-timeline mirror (keeps the current app feed whole)
# ---------------------------------------------------------------------------

def _mirror_activity(ev: dict) -> Optional[tuple[str, dict]]:
    t, d = ev["type"], ev["data"]
    if t == "bark_episode":
        return "bark", {"emotion": d.get("severity"), "duration_s": ev["duration_s"],
                        "bark_count": d.get("bark_count"), "image_url": d.get("image_url")}
    if t == "treat":
        return "treat_dispensed", {"source": d.get("source"), "count": d.get("count", 1)}
    if t == "mission_run":
        return "mission_completed", {"success": d.get("result") == "success",
                                     "mission_type": d.get("mission_type"), "mission_id": d.get("mission_id"),
                                     "result": d.get("result")}
    if t == "intervention":
        return "guardian", {"action": d.get("kind"), "episode_id": d.get("episode_id"), "result": d.get("result")}
    if t == "settle":
        return "guardian", {"action": "settled", "episode_id": d.get("episode_id")}
    if t in ("departure", "arrival"):
        return "guardian", {"action": t, "method": d.get("method")}
    if t == "pacing":
        return "behavior_flag", {"behavior": "pacing", "duration_s": ev["duration_s"], "zone": d.get("zone")}
    if t == "music":
        return "music", {"action": d.get("action"), "track": d.get("track")}
    return None  # status: not a timeline item


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

async def ingest_events(
    events: list[dict],
    *,
    device_id: str,
    user_id: Optional[str],
    source: str = "robot",
    notify: bool = True,
    forward: bool = True,
    dog_id_override: Optional[str] = None,
) -> dict:
    """Run the pipeline for a batch. Returns {accepted, duplicate, rejected}."""
    result: dict[str, list] = {"accepted": [], "duplicate": [], "rejected": []}
    tz_cache: dict[str, ZoneInfo] = {}

    for raw in events:
        ev, reason = validate_event(raw)
        if ev is None:
            result["rejected"].append({"event_id": str((raw or {}).get("event_id", "")), "reason": reason})
            continue
        if dog_id_override:
            ev["dog_id"] = dog_id_override

        # A status with no dog_id is device-scoped: stored once, dog_id NULL, keyed by
        # device_id (no per-dog copies). Status reads go by the dog's paired robot.
        inserted = await _ingest_one(ev, device_id=device_id, user_id=user_id, source=source,
                                     notify=notify, forward=forward, tz_cache=tz_cache)
        result["accepted" if inserted else "duplicate"].append(ev["event_id"])

    return result


async def _ingest_one(ev: dict, *, device_id: str, user_id: Optional[str], source: str, notify: bool,
                      forward: bool, tz_cache: dict) -> bool:
    """Insert one validated event row and run the side effects. Returns False on duplicate."""
    from app.services import notify_service

    dog_id = ev["dog_id"]
    tz_key = dog_id or ""
    tz = tz_cache.get(tz_key)
    if tz is None:
        tz = settings_service.get_timezone(dog_id) if dog_id else ZoneInfo(get_settings().default_timezone)
        tz_cache[tz_key] = tz
    local_date = local_date_for(ev["_dt"], tz)
    ev["local_date"] = local_date
    ev["source"] = source
    ev["device_id"] = device_id
    ev["user_id"] = user_id

    # settle: derive settle_s from the referenced episode so pushes/roll-ups can quote it
    if ev["type"] == "settle" and ev["data"].get("episode_id") and "settle_s" not in ev["data"]:
        episode = get_event(str(ev["data"]["episode_id"]))
        if episode:
            delta = (ev["_dt"] - normalize_ts(episode["ts"])).total_seconds()
            if delta >= 0:
                ev["data"]["settle_s"] = round(delta)

    inserted = insert_event(
        event_id=ev["event_id"], dog_id=dog_id, device_id=device_id, user_id=user_id,
        type=ev["type"], ts=ev["ts"], duration_s=ev["duration_s"], data=ev["data"],
        source=source, local_date=local_date,
    )
    if not inserted:
        return False

    if ev["type"] == "treat" and ev["data"].get("command_id"):
        link_treat_ledger(str(ev["data"]["command_id"]), ev["event_id"])
    if ev["type"] == "treat" and ev["data"].get("image_url"):
        # PHOTO ACCESS: treat stills are kept 7 days (other stills 24h)
        from app.services import media_files
        media_files.pin_media(ev["data"]["image_url"], media_files.TREAT_STILL_TTL_S)

    if ev["type"] == "mode_change":
        # a mode change shifts mode time for that day and every later day it carries into
        dogs = [dog_id] if dog_id else [d["id"] for d in get_user_dogs(user_id or "") if d.get("role") == "owner"]
        mark_summaries_dirty_from(dogs, (ev["_dt"] - timedelta(days=1)).date().isoformat())
    elif ev["type"] != "status":  # status is never rolled up
        mark_summary_dirty(dog_id, local_date)

    mirrored_payload = None
    mirror = _mirror_activity(ev)
    if mirror and user_id:
        act_type, payload = mirror
        payload = {k: v for k, v in payload.items() if v is not None}
        payload["event_id"] = ev["event_id"]
        try:
            stored = insert_activity_event(
                event_id=ev["event_id"], user_id=user_id, device_id=device_id, dog_id=dog_id,
                type=act_type, timestamp=ev["ts"], payload=payload,
            )
            mirrored_payload = (act_type, stored["payload"])
        except Exception as e:  # pragma: no cover
            logger.error(f"[EVENTS] activity mirror failed for {ev['event_id']}: {e}")

    if notify:
        if dog_id:
            try:
                await notify_service.evaluate_notify_rules(ev, user_id)
            except Exception as e:  # pragma: no cover
                logger.error(f"[EVENTS] notify rules failed for {ev['event_id']}: {e}")
            try:
                await notify_service.handle_triggers(ev, user_id)
            except Exception as e:  # pragma: no cover
                logger.error(f"[EVENTS] triggers failed for {ev['event_id']}: {e}")
        _agent_hooks(ev, user_id)

    if forward and user_id:
        await _forward(ev, user_id, mirrored_payload)
    return True


def _agent_hooks(ev: dict, user_id: Optional[str]) -> None:
    """Fire voice-agent hooks for sg_rearm / low_hopper. Fire-and-forget."""
    if ev["type"] != "status":
        return
    mode = str(ev["data"].get("mode") or "")
    prev = _last_mode.get(ev["device_id"])
    _last_mode[ev["device_id"]] = mode
    if mode == "silent_guardian" and prev is not None and prev != "silent_guardian":
        _fire_sg_rearm(ev, prev, user_id)
    tr = ev["data"].get("treats_remaining")
    if isinstance(tr, (int, float)) and tr <= LOW_HOPPER_THRESHOLD:
        _fire_low_hopper(ev, user_id)


def _fire_sg_rearm(ev: dict, prev_mode: str, user_id: Optional[str]) -> None:
    from app.services import notify_service
    # dog_id is None for device-scoped status; the agent resolves the robot's dog
    notify_service.notify_agent("sg_rearm", ev["device_id"], user_id, ev["dog_id"],
                                {"previous_mode": prev_mode, "armed_at": ev["ts"]}, ev["event_id"])


def _fire_low_hopper(ev: dict, user_id: Optional[str]) -> None:
    from app.services import notify_service
    dog_id = ev["dog_id"]
    cap = None
    try:
        if not dog_id:
            raise ValueError("no dog")
        cap = TreatSchedule.model_validate(settings_service.get_value(dog_id, "treat_schedule")).daily_cap
    except Exception:
        pass
    notify_service.notify_agent("low_hopper", ev["device_id"], user_id, dog_id,
                                {"treats_remaining": int(ev["data"]["treats_remaining"]), "treat_daily_cap": cap},
                                ev["event_id"])


def set_last_mode(device_id: str, mode: Optional[str]) -> None:
    """Reset the remembered robot mode (replay jumps), so the next status compares correctly."""
    if mode is None:
        _last_mode.pop(device_id, None)
    else:
        _last_mode[device_id] = mode


def _status_mode(ev: dict) -> Optional[str]:
    m = (ev.get("data") or {}).get("mode")
    return str(m).strip().lower() if m else None


async def catch_up_skipped(skipped: list[dict], prior: list[dict], *, device_id: str,
                           user_id: Optional[str]) -> list[dict]:
    """After a silent replay jump, fire what the newest skipped events would have fired.

    skipped: the events just stored (chronological); prior: events before them (chronological).
    - notify rules: for each enabled rule, its newest matching skipped event (one push per rule;
      the rule's attach still/clip comes from that event).
    - sg_rearm: the last switch into silent_guardian, only if the robot is still in it at the
      landing point.
    - low_hopper: only if treats_remaining crossed down to the threshold in the skipped span
      and is still at or below it at the landing point.
    Also leaves the remembered robot mode at the landing point. Returns what was fired."""
    from app.services import notify_service

    fired: list[dict] = []
    picks: dict[str, tuple[dict, set[str]]] = {}
    for dog_id in {e["dog_id"] for e in skipped if e.get("dog_id") and e["type"] != "status"}:
        evs = [e for e in skipped if e.get("dog_id") == dog_id and e["type"] != "status"]
        rules = NotifyRules.model_validate(settings_service.get_value(dog_id, "notify_rules"))
        for rule in rules.rules:
            if not rule.enabled:
                continue
            ev = next((e for e in reversed(evs) if e["type"] == rule.event_type
                       and notify_service._matches(rule, e)), None)
            if ev is not None:
                picks.setdefault(ev["event_id"], (ev, set()))[1].add(rule.id)
    for ev, rule_ids in sorted(picks.values(), key=lambda p: p[0]["ts"]):
        try:
            outcomes = await notify_service.evaluate_notify_rules(ev, user_id, only_rule_ids=rule_ids)
        except Exception as e:  # pragma: no cover
            logger.error(f"[EVENTS] catch-up notify failed for {ev['event_id']}: {e}")
            continue
        fired.append({"kind": "notify", "event_id": ev["event_id"], "type": ev["type"], "outcomes": outcomes})

    statuses = [e for e in skipped if e["type"] == "status"]
    if not statuses:
        return fired
    prior_status = [e for e in prior if e.get("type") == "status"]
    mode = next((m for m in (_status_mode(e) for e in reversed(prior_status)) if m), None) \
        if prior_status else _last_mode.get(device_id)
    tr_prev = next((e["data"]["treats_remaining"] for e in reversed(prior_status)
                    if isinstance((e.get("data") or {}).get("treats_remaining"), (int, float))), None)
    rearm: Optional[tuple[dict, str]] = None
    crossed, last_tr_ev = False, None
    for e in statuses:
        m = _status_mode(e)
        if m:
            if m == "silent_guardian" and mode is not None and mode != "silent_guardian":
                rearm = (e, mode)
            mode = m
        tr = e["data"].get("treats_remaining")
        if isinstance(tr, (int, float)):
            if tr <= LOW_HOPPER_THRESHOLD and (tr_prev is None or tr_prev > LOW_HOPPER_THRESHOLD):
                crossed = True
            tr_prev, last_tr_ev = tr, e
    set_last_mode(device_id, mode)
    if rearm and mode == "silent_guardian":
        _fire_sg_rearm({**rearm[0], "device_id": device_id}, rearm[1], user_id)
        fired.append({"kind": "sg_rearm", "event_id": rearm[0]["event_id"], "previous_mode": rearm[1]})
    if crossed and last_tr_ev is not None and tr_prev is not None and tr_prev <= LOW_HOPPER_THRESHOLD:
        _fire_low_hopper({**last_tr_ev, "device_id": device_id}, user_id)
        fired.append({"kind": "low_hopper", "event_id": last_tr_ev["event_id"], "treats_remaining": int(tr_prev)})
    return fired


async def _forward(ev: dict, user_id: str, mirrored: Optional[tuple[str, dict]]) -> None:
    from app.connection_manager import get_connection_manager

    manager = get_connection_manager()
    public = {k: ev[k] for k in ("event_id", "type", "dog_id", "device_id", "ts", "duration_s", "data", "source")}
    if ev["type"] != "status":
        await manager.send_to_user_apps(user_id, {"type": "event", "event": public})
    if mirrored:
        act_type, payload = mirrored
        await manager.send_to_user_apps(user_id, {
            "type": "activity_event",
            "id": ev["event_id"],
            "device_id": ev["device_id"],
            "dog_id": ev["dog_id"],
            "event_type": act_type,
            "timestamp": ev["ts"],
            "payload": payload,
        })


# ---------------------------------------------------------------------------
# Daily roll-up
# ---------------------------------------------------------------------------

def resolve_date(dog_id: str, token: str) -> str:
    token = (token or "today").lower()
    today = settings_service.local_now(dog_id).date()
    if token == "today":
        return today.isoformat()
    if token == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        return datetime.strptime(token, "%Y-%m-%d").date().isoformat()
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail={"code": "bad_date", "reason": "use YYYY-MM-DD, today or yesterday"})


def _parse(ts: str) -> datetime:
    return normalize_ts(ts) or datetime.now(timezone.utc)


def _device_mode_periods(device_id: str, start: datetime, end: datetime) -> tuple[list[tuple], bool]:
    """(mode, dog_id, from, to) periods for one robot within [start, end), from its mode_change events.
    The mode in force at `start` carries in from the last earlier mode_change. A period also ends at a
    status heartbeat reporting a different mode, and HEARTBEAT_GRACE_S after the last heartbeat (robot
    went quiet). Returns (periods, still_running_at_end)."""
    s_iso, e_iso = start.isoformat(), end.isoformat()
    prior = last_device_event_before(device_id, "mode_change", s_iso)
    rows = list_device_events(device_id, ["mode_change", "status"], since=s_iso, until=e_iso)
    beats = [r for r in rows if r["type"] == "status"]
    last_beat = beats[-1] if beats else last_device_event_before(device_id, "status", s_iso)
    stop = end   # a robot that never sends status heartbeats is not capped
    if last_beat:
        stop = min(end, max(start, _parse(last_beat["ts"]) + timedelta(seconds=HEARTBEAT_GRACE_S)))
    periods: list[tuple] = []
    cur: Optional[tuple] = None   # (mode, dog_id, from)
    for r in ([prior] if prior else []) + rows:
        t = max(_parse(r["ts"]), start)
        mode = str((r["data"] or {}).get("mode") or "").lower()
        if r["type"] == "mode_change":
            if cur:
                periods.append((*cur, t))
            cur = (mode, r["dog_id"], t)
        elif cur and mode and mode != cur[0]:   # heartbeat contradicts the open period
            periods.append((*cur, t))
            cur = None
    running = False
    if cur:
        periods.append((*cur, max(cur[2], stop)))
        running = stop >= end
    return periods, running


def mode_seconds(dog_id: str, start: datetime, end_of_day: datetime, now: datetime) -> tuple[dict, bool]:
    """Seconds the owner's robots spent in each MODE_TOTALS mode on [start, end_of_day), counting
    periods whose mode_change named no dog or named this dog. Overlapping robots are counted once."""
    out = {k: 0 for k in MODE_TOTALS.values()}
    end = min(now, end_of_day)
    owner = get_dog_owner(dog_id)
    if end <= start or not owner:
        return out, False
    spans: dict[str, list[tuple[datetime, datetime]]] = {k: [] for k in out}
    running = False
    for device_id in get_user_paired_devices(owner):
        periods, still = _device_mode_periods(device_id, start, end)
        for mode, ev_dog, a, b in periods:
            key = MODE_TOTALS.get(mode)
            if key and (ev_dog is None or ev_dog == dog_id) and b > a:
                spans[key].append((a, b))
                running = running or (still and b >= end and end < end_of_day)
    for key, iv in spans.items():
        total, last_end = 0.0, None
        for a, b in sorted(iv):
            if last_end is not None and a < last_end:
                a = last_end
            if b > a:
                total += (b - a).total_seconds()
                last_end = b if last_end is None else max(last_end, b)
        out[key] = round(total)
    return out, running


def cue_rates(cues: int, successes: int) -> dict:
    """success_rate (0-1, 2 dp) and success_pct (whole percent) for a cue tally; None with no cues."""
    if not cues:
        return {"success_rate": None, "success_pct": None}
    return {"success_rate": round(successes / cues, 2), "success_pct": round(100 * successes / cues)}


def compute_daily_summary(dog_id: str, date_str: str) -> dict:
    tz = settings_service.get_timezone(dog_id)
    rows = list_events(dog_id, local_date=date_str, limit=20000, order="asc")
    rows = [r for r in rows if r["type"] not in ("status", "mode_change")]

    counts = {t: 0 for t in EVENT_TYPES if t not in ("status", "mode_change")}
    totals: dict[str, Any] = {
        "bark_s": 0.0, "pacing_s": 0.0, "music_s": 0.0, "treats": 0, "alone_s": 0.0,
        "interventions_by_kind": {}, "missions_success": 0, "missions_fail": 0,
        "cues": 0, "cue_successes": 0, "cues_by_trick": {},
    }
    session_s = 0.0
    for r in rows:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
        d, dur = r["data"], r["duration_s"] or 0.0
        if r["type"] == "bark_episode":
            totals["bark_s"] += dur
        elif r["type"] == "pacing":
            totals["pacing_s"] += dur
        elif r["type"] == "music" and d.get("action") == "stop":
            totals["music_s"] += dur
        elif r["type"] == "treat":
            totals["treats"] += int(d.get("count", 1) or 1)
        elif r["type"] == "intervention":
            k = str(d.get("kind"))
            totals["interventions_by_kind"][k] = totals["interventions_by_kind"].get(k, 0) + 1
        elif r["type"] == "mission_run":
            if d.get("result") == "success":
                totals["missions_success"] += 1
            elif d.get("result") == "fail":
                totals["missions_fail"] += 1
        elif r["type"] == "coach_session":
            session_s += dur
        elif r["type"] == "trick_attempt":
            per = totals["cues_by_trick"].setdefault(str(d.get("trick")), {"cues": 0, "successes": 0})
            per["cues"] += 1
            totals["cues"] += 1
            if d.get("success"):
                per["successes"] += 1
                totals["cue_successes"] += 1

    # alone time: departure -> next arrival, clipped to the local day
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    now = datetime.now(timezone.utc)
    open_departure: Optional[datetime] = None
    for r in rows:
        if r["type"] == "departure" and open_departure is None:
            open_departure = _parse(r["ts"])
        elif r["type"] == "arrival" and open_departure is not None:
            totals["alone_s"] += max(0.0, (_parse(r["ts"]) - open_departure).total_seconds())
            open_departure = None
    if open_departure is not None:
        end = min(now, day_end.astimezone(timezone.utc))
        totals["alone_s"] += max(0.0, (end - open_departure).total_seconds())

    # time to settle + episode attribution
    episodes = [r for r in rows if r["type"] == "bark_episode"]
    settles = [r for r in rows if r["type"] == "settle"]
    interventions = [r for r in rows if r["type"] == "intervention"]
    used_settles: set[str] = set()
    settle_times: list[float] = []
    unresolved = 0
    top: list[dict] = []
    for e in episodes:
        e_start = _parse(e["ts"])
        e_end = e_start + timedelta(seconds=(e["duration_s"] or 0) + SETTLE_WINDOW_S)
        match = None
        for s in settles:
            if s["event_id"] in used_settles:
                continue
            if s["data"].get("episode_id") == e["event_id"]:
                match = s
                break
        if match is None:
            for s in settles:
                if s["event_id"] in used_settles or s["data"].get("episode_id"):
                    continue
                s_t = _parse(s["ts"])
                if e_start <= s_t <= e_end:
                    match = s
                    break
        settle_s = None
        if match is not None:
            used_settles.add(match["event_id"])
            settle_s = (_parse(match["ts"]) - e_start).total_seconds()
            if settle_s < 0 and match["duration_s"]:
                settle_s = float(match["duration_s"])
            settle_s = max(0.0, settle_s)
            settle_times.append(settle_s)
        else:
            unresolved += 1
        window_end = _parse(match["ts"]) if match is not None else e_end
        kinds = [
            i["data"].get("kind") for i in interventions
            if i["data"].get("episode_id") == e["event_id"]
            or (not i["data"].get("episode_id") and e_start <= _parse(i["ts"]) <= window_end)
        ]
        top.append({
            "event_id": e["event_id"], "ts": e["ts"], "duration_s": e["duration_s"],
            "bark_count": e["data"].get("bark_count"), "severity": e["data"].get("severity"),
            "settle_s": round(settle_s) if settle_s is not None else None,
            "interventions": kinds, "image_url": e["data"].get("image_url"),
        })
    top.sort(key=lambda x: (-(x["duration_s"] or 0), -(x["bark_count"] or 0), x["ts"]))

    tts = {"n": len(settle_times), "unresolved": unresolved,
           "median_s": round(statistics.median(settle_times)) if settle_times else None,
           "mean_s": round(statistics.fmean(settle_times)) if settle_times else None,
           "min_s": round(min(settle_times)) if settle_times else None,
           "max_s": round(max(settle_times)) if settle_times else None}

    try:
        totals["treat_cap"] = TreatSchedule.model_validate(
            settings_service.get_value(dog_id, "treat_schedule")).daily_cap
    except Exception:
        totals["treat_cap"] = None
    mode_s, mode_running = mode_seconds(dog_id, day_start.astimezone(timezone.utc),
                                        day_end.astimezone(timezone.utc), now)
    totals.update(mode_s)
    if counts.get("coach_session"):
        # explicit Coach sessions are the coaching time; otherwise time in Coach mode (mode_change)
        totals["coach_s"] = round(session_s)
    totals.update(cue_rates(totals["cues"], totals["cue_successes"]))
    for k in ("bark_s", "pacing_s", "music_s", "alone_s"):
        totals[k] = round(totals[k])

    return {
        "dog_id": dog_id, "date": date_str, "tz": str(tz.key),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts, "totals": totals, "time_to_settle": tts, "top_episodes": top[:3],
        "event_count": len(rows),
        "mode_running": mode_running,   # a counted mode is still on: recompute on the next read
        "first_event_ts": rows[0]["ts"] if rows else None,
        "last_event_ts": rows[-1]["ts"] if rows else None,
    }


def get_summary(dog_id: str, date_str: str, refresh: bool = False) -> dict:
    row = None if refresh else get_daily_summary_row(dog_id, date_str)
    if row and not row["dirty"] and not row["summary"].get("mode_running"):
        return row["summary"]
    summary = compute_daily_summary(dog_id, date_str)
    upsert_daily_summary(dog_id, date_str, summary["tz"], summary, summary["event_count"],
                         summary["last_event_ts"])
    return summary


# ---- weekly roll-up (2026-11-08) ----

def week_start_of(date_str: str) -> str:
    """The Monday (local date) of the week containing date_str."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _sum_into(acc: dict, add: dict) -> None:
    for k, v in add.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
        elif isinstance(v, dict):
            _sum_into(acc.setdefault(k, {}), v)


def combine_daily_summaries(dog_id: str, week_start: str, dailies: list[dict], tz: str) -> dict:
    """Add up a week's daily summaries into one summary of the same shape.

    counts and totals are summed (interventions_by_kind per kind); treat_cap stays the
    per-day cap (latest day's). time_to_settle: n, unresolved and min/max are exact and
    mean_s is the n-weighted mean; median_s is the n-weighted median of the daily medians
    (daily summaries do not keep every settle time). top_episodes: the week's top 3."""
    ws = datetime.strptime(week_start, "%Y-%m-%d").date()
    counts: dict = {}
    totals: dict = {}
    top: list[dict] = []
    medians: list[float] = []
    n = unresolved = 0
    mean_acc = 0.0
    mins, maxs, caps = [], [], []
    for s in dailies:
        _sum_into(counts, s.get("counts") or {})
        t = dict(s.get("totals") or {})
        caps.append(t.pop("treat_cap", None))
        t.pop("success_rate", None), t.pop("success_pct", None)     # ratios: recomputed below
        _sum_into(totals, t)
        top.extend(s.get("top_episodes") or [])
        tts = s.get("time_to_settle") or {}
        dn = tts.get("n") or 0
        n += dn
        unresolved += tts.get("unresolved") or 0
        if dn and tts.get("mean_s") is not None:
            mean_acc += tts["mean_s"] * dn
        if dn and tts.get("median_s") is not None:
            medians.extend([tts["median_s"]] * dn)
        if tts.get("min_s") is not None:
            mins.append(tts["min_s"])
        if tts.get("max_s") is not None:
            maxs.append(tts["max_s"])
    totals["treat_cap"] = next((c for c in reversed(caps) if c is not None), None)
    totals.update(cue_rates(totals.get("cues", 0), totals.get("cue_successes", 0)))
    top.sort(key=lambda x: (-(x.get("duration_s") or 0), -(x.get("bark_count") or 0), x.get("ts") or ""))
    firsts = [s["first_event_ts"] for s in dailies if s.get("first_event_ts")]
    lasts = [s["last_event_ts"] for s in dailies if s.get("last_event_ts")]
    return {
        "dog_id": dog_id, "week_start": week_start, "date": week_start,
        "week_end": (ws + timedelta(days=6)).isoformat(),
        "days": [s.get("date") for s in dailies], "tz": tz,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts, "totals": totals,
        "time_to_settle": {"n": n, "unresolved": unresolved,
                           "median_s": round(statistics.median(medians)) if medians else None,
                           "mean_s": round(mean_acc / n) if n else None,
                           "min_s": min(mins) if mins else None, "max_s": max(maxs) if maxs else None},
        "top_episodes": top[:3],
        "event_count": sum(s.get("event_count") or 0 for s in dailies),
        "mode_running": any(s.get("mode_running") for s in dailies),
        "first_event_ts": min(firsts) if firsts else None,
        "last_event_ts": max(lasts) if lasts else None,
    }


def get_weekly_summary(dog_id: str, date_str: str, refresh: bool = False) -> dict:
    """Weekly roll-up for the week (Monday-Sunday, local) containing date_str.

    Built from get_summary() for each day of the week up to today (the current week is
    partial). Cached in weekly_summary once the week is over, and served from the cache
    while every daily row of that week is clean and no newer than the weekly row."""
    week_start = week_start_of(date_str)
    ws = datetime.strptime(week_start, "%Y-%m-%d").date()
    week_end = (ws + timedelta(days=6)).isoformat()
    today = settings_service.local_now(dog_id).date()
    complete = ws + timedelta(days=6) < today
    if complete and not refresh:
        row = get_weekly_summary_row(dog_id, week_start)
        if (row and not row["dirty"] and not row["summary"].get("mode_running")
                and daily_rows_fresh_for_week(dog_id, week_start, week_end, row["computed_at"])):
            return row["summary"]
    days = [(ws + timedelta(days=i)).isoformat() for i in range(7) if ws + timedelta(days=i) <= today]
    dailies = [get_summary(dog_id, d, refresh=refresh) for d in days]
    tz = dailies[0]["tz"] if dailies else str(settings_service.get_timezone(dog_id).key)
    summary = combine_daily_summaries(dog_id, week_start, dailies, tz)
    if complete:
        upsert_weekly_summary(dog_id, week_start, tz, summary, summary["event_count"], summary["last_event_ts"])
    return summary


# ---- trick history (2026-11-08) ----

def trick_history(dog_id: str, days: int = 30, recent: int = 10) -> dict:
    """Per-trick record from trick_attempt events over the last `days` local days (today included):
    attempts, successes, success_rate / success_pct, average latency and the last attempt; plus
    the `recent` newest attempts and overall totals."""
    days = max(1, min(int(days), 730))
    recent = max(0, min(int(recent), 50))
    tz = settings_service.get_timezone(dog_id)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(timezone.utc).isoformat()
    rows = list_events(dog_id, since=since, types=["trick_attempt"], limit=50000, order="asc")
    per: dict[str, dict] = {}
    for r in rows:
        d = r["data"]
        t = per.setdefault(str(d.get("trick")), {"trick": str(d.get("trick")), "attempts": 0, "successes": 0,
                                                 "_lat": []})
        t["attempts"] += 1
        t["successes"] += 1 if d.get("success") else 0
        if isinstance(d.get("latency_ms"), int):
            t["_lat"].append(d["latency_ms"])
        t["last_attempt_ts"], t["last_result"] = r["ts"], d.get("result")
    tricks = []
    for t in per.values():
        lat = t.pop("_lat")
        t.update(cue_rates(t["attempts"], t["successes"]))
        t["avg_latency_ms"] = round(sum(lat) / len(lat)) if lat else None
        tricks.append(t)
    tricks.sort(key=lambda x: (-x["attempts"], x["trick"]))
    attempts = sum(t["attempts"] for t in tricks)
    successes = sum(t["successes"] for t in tricks)
    return {
        "dog_id": dog_id, "days": days, "from": first.isoformat(), "to": today.isoformat(), "tz": str(tz.key),
        "tricks": tricks,
        "recent": [{"event_id": r["event_id"], "ts": r["ts"], "trick": r["data"].get("trick"),
                    "result": r["data"].get("result"), "latency_ms": r["data"].get("latency_ms"),
                    "session_id": r["data"].get("session_id")} for r in reversed(rows[-recent:] if recent else [])],
        "totals": {"attempts": attempts, "successes": successes, **cue_rates(attempts, successes)},
    }
