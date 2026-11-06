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
    get_daily_summary_row,
    get_event,
    insert_activity_event,
    insert_event,
    link_treat_ledger,
    list_events,
    mark_summary_dirty,
    upsert_daily_summary,
)
from app.models import EVENT_TYPES, TreatSchedule
from app.services import settings_service

logger = logging.getLogger(__name__)

MEDIA_PREFIX = "/api/media/download/"
SETTLE_WINDOW_S = 600  # a settle within episode end + 10 min counts for that episode
LOW_HOPPER_THRESHOLD = 3

# last known robot mode per dog (for sg_rearm detection); in-process only
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
    dog_id = str(ev.get("dog_id") or "").strip()
    if not dog_id:
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
    from app.services import notify_service

    result: dict[str, list] = {"accepted": [], "duplicate": [], "rejected": []}
    tz_cache: dict[str, ZoneInfo] = {}

    for raw in events:
        ev, reason = validate_event(raw)
        if ev is None:
            result["rejected"].append({"event_id": str((raw or {}).get("event_id", "")), "reason": reason})
            continue
        if dog_id_override:
            ev["dog_id"] = dog_id_override
        dog_id = ev["dog_id"]
        tz = tz_cache.get(dog_id)
        if tz is None:
            tz = settings_service.get_timezone(dog_id)
            tz_cache[dog_id] = tz
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
            result["duplicate"].append(ev["event_id"])
            continue
        result["accepted"].append(ev["event_id"])

        if ev["type"] == "treat" and ev["data"].get("command_id"):
            link_treat_ledger(str(ev["data"]["command_id"]), ev["event_id"])
        if ev["type"] == "treat" and ev["data"].get("image_url"):
            # PHOTO ACCESS: treat stills are kept 7 days (other stills 24h)
            from app.services import media_files
            media_files.pin_media(ev["data"]["image_url"], media_files.TREAT_STILL_TTL_S)

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

    return result


def _agent_hooks(ev: dict, user_id: Optional[str]) -> None:
    """Fire voice-agent hooks for sg_rearm / low_hopper. Fire-and-forget."""
    from app.services import notify_service

    if ev["type"] != "status":
        return
    dog_id = ev["dog_id"]
    mode = str(ev["data"].get("mode") or "")
    prev = _last_mode.get(dog_id)
    _last_mode[dog_id] = mode
    if mode == "silent_guardian" and prev is not None and prev != "silent_guardian":
        notify_service.notify_agent("sg_rearm", ev["device_id"], user_id, dog_id,
                                    {"previous_mode": prev, "armed_at": ev["ts"]}, ev["event_id"])
    tr = ev["data"].get("treats_remaining")
    if isinstance(tr, (int, float)) and tr <= LOW_HOPPER_THRESHOLD:
        cap = None
        try:
            cap = TreatSchedule.model_validate(settings_service.get_value(dog_id, "treat_schedule")).daily_cap
        except Exception:
            pass
        notify_service.notify_agent("low_hopper", ev["device_id"], user_id, dog_id,
                                    {"treats_remaining": int(tr), "treat_daily_cap": cap}, ev["event_id"])


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


def compute_daily_summary(dog_id: str, date_str: str) -> dict:
    tz = settings_service.get_timezone(dog_id)
    rows = list_events(dog_id, local_date=date_str, limit=20000, order="asc")
    rows = [r for r in rows if r["type"] != "status"]

    counts = {t: 0 for t in EVENT_TYPES if t != "status"}
    totals: dict[str, Any] = {
        "bark_s": 0.0, "pacing_s": 0.0, "music_s": 0.0, "treats": 0, "alone_s": 0.0,
        "interventions_by_kind": {}, "missions_success": 0, "missions_fail": 0,
    }
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
    for k in ("bark_s", "pacing_s", "music_s", "alone_s"):
        totals[k] = round(totals[k])

    return {
        "dog_id": dog_id, "date": date_str, "tz": str(tz.key),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts, "totals": totals, "time_to_settle": tts, "top_episodes": top[:3],
        "event_count": len(rows),
        "first_event_ts": rows[0]["ts"] if rows else None,
        "last_event_ts": rows[-1]["ts"] if rows else None,
    }


def get_summary(dog_id: str, date_str: str, refresh: bool = False) -> dict:
    row = None if refresh else get_daily_summary_row(dog_id, date_str)
    if row and not row["dirty"]:
        return row["summary"]
    summary = compute_daily_summary(dog_id, date_str)
    upsert_daily_summary(dog_id, date_str, summary["tz"], summary, summary["event_count"],
                         summary["last_event_ts"])
    return summary
