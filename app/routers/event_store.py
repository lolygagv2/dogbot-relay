"""Event store routes (Dec 9 demo build, 2026-11-06).

POST /events                      robot (HMAC) or admin: batch ingest
GET  /summary/{dog_id}/{date}     daily roll-up (date = YYYY-MM-DD | today | yesterday)
GET  /summary/{dog_id}/week/{date} weekly roll-up for the Monday-Sunday week containing date
GET  /api/dogs/{dog_id}/tricks/history   per-trick record from trick_attempt events
GET  /api/dogs/{dog_id}/events    raw events (status excluded unless requested)
GET  /api/dogs/{dog_id}/status    latest robot status + treat/quiet-hours context
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.auth import Actor, check_dog_access, resolve_actor
from app.database import get_event, get_latest_status_for_devices, get_user_paired_devices, list_events
from app.services import media_files
from app.models import EVENT_TYPES, EventBatchIn, EventBatchResult, QuietHours
from app.services import event_store, pairing, settings_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Event store"])


@router.post("/events", response_model=EventBatchResult)
async def post_events(batch: EventBatchIn, actor: Actor = Depends(resolve_actor)):
    if actor.kind not in ("robot", "admin"):
        raise HTTPException(status_code=403, detail={"code": "robot_or_admin_only"})
    if actor.kind == "robot":
        device_id, user_id = actor.device_id, actor.user_id
    else:
        # admin curl path: device from the first event or the admin's first paired robot
        device_id = batch.events[0].device_id or next(iter(get_user_paired_devices(actor.user_id)), "admin")
        user_id = actor.user_id

    # Robot may only post for dogs its owner has; admin may post for anything.
    # A robot that omits dog_id: with exactly one dog the relay fills it in (A5); with several the
    # row is stored with dog_id NULL (ROBOT-STATE v2 rule A6): it counts in no per-dog total and the
    # relay never guesses. Status and mode_change are always device-scoped (dog_id NULL).
    owner_dogs = pairing.device_dog_ids(device_id) if actor.kind == "robot" else []
    rejected, allowed = [], []
    for ev in batch.events:
        try:
            item = ev.model_dump()
            if item["dog_id"] is None and ev.type not in ("status", "mode_change") and len(owner_dogs) == 1:
                item["dog_id"] = owner_dogs[0]
            if actor.kind == "robot" and item["dog_id"] is not None:
                check_dog_access(actor, item["dog_id"])
            allowed.append(item)
        except HTTPException as e:
            rejected.append({"event_id": ev.event_id, "reason": str(e.detail.get("code") if isinstance(e.detail, dict) else e.detail)})

    result = await event_store.ingest_events(allowed, device_id=device_id, user_id=user_id, source="robot")
    result["rejected"].extend(rejected)
    logger.info(f"[EVENTS] {actor.label}: accepted={len(result['accepted'])} dup={len(result['duplicate'])} "
                f"rejected={len(result['rejected'])}")
    return result


@router.get("/summary/{dog_id}/{date}")
async def get_summary(dog_id: str, date: str, refresh: bool = Query(default=False),
                      actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    date_str = event_store.resolve_date(dog_id, date)
    return event_store.get_summary(dog_id, date_str, refresh=refresh)


@router.get("/summary/{dog_id}/week/{date}")
async def get_week_summary(dog_id: str, date: str, refresh: bool = Query(default=False),
                           actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    date_str = event_store.resolve_date(dog_id, date)
    return event_store.get_weekly_summary(dog_id, date_str, refresh=refresh)


@router.get("/api/dogs/{dog_id}/tricks/history")
async def get_trick_history(dog_id: str, days: int = Query(default=30, ge=1, le=730),
                            recent: int = Query(default=10, ge=0, le=50),
                            actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    return event_store.trick_history(dog_id, days=days, recent=recent)


@router.get("/api/dogs/{dog_id}/events")
async def get_dog_events(
    dog_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    type: Optional[str] = Query(default=None, description="comma-separated event types"),
    limit: int = Query(default=100, ge=1, le=500),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    actor: Actor = Depends(resolve_actor),
):
    check_dog_access(actor, dog_id)
    types = [t.strip() for t in type.split(",") if t.strip()] if type else None
    if types:
        bad = [t for t in types if t not in EVENT_TYPES]
        if bad:
            raise HTTPException(status_code=422, detail={"code": "unknown_type", "types": bad})
    else:
        types = [t for t in EVENT_TYPES if t not in ("status", "mode_change")]
    for name, val in (("since", since), ("until", until)):
        if val and event_store.normalize_ts(val) is None:
            raise HTTPException(status_code=422, detail={"code": "bad_timestamp", "field": name})
    since_n = event_store.normalize_ts(since).isoformat() if since else None
    until_n = event_store.normalize_ts(until).isoformat() if until else None
    rows = list_events(dog_id, since=since_n, until=until_n, types=types, limit=limit, order=order)
    for r in rows:
        r.pop("user_id", None)
        r.pop("local_date", None)
    return {"dog_id": dog_id, "events": rows, "count": len(rows)}


@router.get("/api/dogs/{dog_id}/status")
async def get_dog_status(dog_id: str, actor: Actor = Depends(resolve_actor)):
    from app.connection_manager import get_connection_manager

    owner = check_dog_access(actor, dog_id)
    manager = get_connection_manager()
    devices = pairing.dog_devices(dog_id, owner)
    online = [d for d in devices if manager.is_robot_online(d)]
    replay_active, replay_device, replay_vt, replay_date, replay_mode = False, None, None, None, None
    try:
        from app.services.replayer import get_replayer
        rp = get_replayer()
        replay_active = rp.handles(dog_id)
        if replay_active:   # the replay's clock, for this dog only (2026-11-18)
            replay_device, replay_date = rp.device_id, rp.date
            replay_vt = rp.virtual_time().isoformat()
            replay_mode = rp.virtual_mode()
    except ImportError:
        pass
    # Robot state is the robot's own: "online" = one of the owner's robots is connected, no
    # matter which dog is asked about. The latest status row is read by device_id (an online
    # robot first), never by dog_id (contract fix 2026-11-06). ROBOT-STATE v2: mode / asleep /
    # dogs come from the relay's state record for that robot, never from a replay.
    candidates = online or devices
    if replay_device:
        candidates = [*candidates, replay_device]
    last = get_latest_status_for_devices(candidates)
    status_device = (last or {}).get("device_id")
    if status_device not in devices:   # a replay's demo robot is never the state's robot
        status_device = online[0] if online else (devices[0] if devices else None)
    from app.services import robot_state, sleep_wake
    st = robot_state.get(status_device) if status_device else None
    used, _ = settings_service.treats_used_today(dog_id)
    cap = settings_service.effective_daily_cap(settings_service.get_value(dog_id, "treat_schedule"))
    qh = QuietHours.model_validate(settings_service.get_value(dog_id, "quiet_hours"))
    return {
        "dog_id": dog_id,
        "device_id": status_device,
        "robot_online": bool(online),
        "mode": st["mode"] if st else None,
        "asleep": st["asleep"] if st else False,
        "waking": bool(status_device and sleep_wake.is_waking(status_device)),
        "selected_dog_id": st["selected_dog_id"] if st else None,
        "selected_dog_applied": st["selected_dog_applied"] if st else False,
        "session_dog_id": st["session_dog_id"] if st else None,
        "state_version": st["state_version"] if st else None,
        "battery": (last or {}).get("data", {}).get("battery"),
        "last_status": (last or {}).get("data"),
        "last_status_ts": (last or {}).get("ts"),
        "treats_today": used,
        "treat_cap": cap,
        "quiet_hours": {"enabled": qh.enabled, "start": qh.start, "end": qh.end,
                        "active": settings_service.in_quiet_hours(dog_id)},
        "timezone": settings_service.get_value(dog_id, "timezone"),
        # "Barking down X% vs last week": today so far vs last week's average day to the same
        # time (replay clock during a replay); None when last week has nothing to compare with
        "bark_vs_last_week": event_store.bark_vs_last_week(
            dog_id, datetime.fromisoformat(replay_vt) if replay_vt else None),
        # replay data only here (ROBOT-STATE v2, section 8); null unless this dog's replay is loaded
        "replay": {"active": replay_active, "virtual_mode": replay_mode, "virtual_time": replay_vt,
                   "date": replay_date},
    }


# ---- PHOTO ACCESS (amendment 2026-11-06): stills attached to events, never video ----

@router.get("/api/dogs/{dog_id}/events/{event_id}/photo")
async def get_event_photo(dog_id: str, event_id: str, actor: Actor = Depends(resolve_actor)):
    """The still attached to an event (treat, bark_episode, intervention). App, connector, agent, admin."""
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_not_allowed"})
    check_dog_access(actor, dog_id)
    ev = get_event(event_id)
    if not ev or ev["dog_id"] != dog_id:
        raise HTTPException(status_code=404, detail={"code": "event_not_found"})
    name = media_files.filename_from_url(ev["data"].get("image_url"))
    if not name:
        raise HTTPException(status_code=404, detail={"code": "no_photo_for_event"})
    path = media_files.resolve(name)
    if not path:
        raise HTTPException(status_code=410, detail={"code": "photo_expired"})
    return FileResponse(str(path), media_type=media_files.media_type_for(path),
                        headers={"Cache-Control": "private, max-age=300", "X-Event-Id": event_id})


@router.get("/api/dogs/{dog_id}/media/{filename}")
async def get_dog_media_still(dog_id: str, filename: str, actor: Actor = Depends(resolve_actor)):
    """A still (after take_photo) or a listen() audio clip, by download name. Images and audio only;
    the name must come from a robot paired to the dog's owner (upload names are
    <device_id>_<hex>.<ext>) or a seed. Expired files (stills 24 h, clips 1 h) are 404."""
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_not_allowed"})
    owner = check_dog_access(actor, dog_id)
    safe = filename.split("/")[-1]
    path = media_files.resolve(safe)
    if not path or path.suffix.lower() not in (media_files.IMAGE_EXTS | media_files.AUDIO_EXTS):
        raise HTTPException(status_code=404, detail={"code": "still_not_found"})
    devices = get_user_paired_devices(owner) if owner else []
    if not safe.startswith("seed_") and not any(safe.startswith(f"{d}_") for d in devices):
        raise HTTPException(status_code=403, detail={"code": "still_not_owned"})
    return FileResponse(str(path), media_type=media_files.media_type_for(path),
                        headers={"Cache-Control": "private, max-age=300"})
