"""Event store routes (Dec 9 demo build, 2026-11-06).

POST /events                      robot (HMAC) or admin: batch ingest
GET  /summary/{dog_id}/{date}     daily roll-up (date = YYYY-MM-DD | today | yesterday)
GET  /api/dogs/{dog_id}/events    raw events (status excluded unless requested)
GET  /api/dogs/{dog_id}/status    latest robot status + treat/quiet-hours context
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.auth import Actor, check_dog_access, resolve_actor
from app.database import get_event, get_latest_event, get_user_paired_devices, list_events
from app.services import media_files
from app.models import EVENT_TYPES, EventBatchIn, EventBatchResult, QuietHours
from app.services import event_store, settings_service

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
    rejected, allowed = [], []
    for ev in batch.events:
        try:
            if actor.kind == "robot":
                check_dog_access(actor, ev.dog_id)
            allowed.append(ev.model_dump())
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
        types = [t for t in EVENT_TYPES if t != "status"]
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
    devices = get_user_paired_devices(owner) if owner else []
    online = [d for d in devices if manager.is_robot_online(d)]
    last = get_latest_event(dog_id, "status")
    used, _ = settings_service.treats_used_today(dog_id)
    cap = settings_service.get_value(dog_id, "treat_schedule").get("daily_cap")
    qh = QuietHours.model_validate(settings_service.get_value(dog_id, "quiet_hours"))
    replay_active = False
    try:
        from app.services.replayer import get_replayer
        replay_active = get_replayer().handles(dog_id)
    except ImportError:
        pass
    return {
        "dog_id": dog_id,
        "device_id": online[0] if online else (devices[0] if devices else None),
        "robot_online": bool(online),
        "mode": (last or {}).get("data", {}).get("mode"),
        "battery": (last or {}).get("data", {}).get("battery"),
        "last_status": (last or {}).get("data"),
        "last_status_ts": (last or {}).get("ts"),
        "treats_today": used,
        "treat_cap": cap,
        "quiet_hours": {"enabled": qh.enabled, "start": qh.start, "end": qh.end,
                        "active": settings_service.in_quiet_hours(dog_id)},
        "timezone": settings_service.get_value(dog_id, "timezone"),
        "replay_active": replay_active,
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
    """A still by download name (used right after take_photo). Images only; the name must come
    from a robot paired to the dog's owner (upload names are <device_id>_<hex>.<ext>) or a seed."""
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_not_allowed"})
    owner = check_dog_access(actor, dog_id)
    safe = filename.split("/")[-1]
    path = media_files.resolve(safe)
    if not path or path.suffix.lower() not in media_files.IMAGE_EXTS:
        raise HTTPException(status_code=404, detail={"code": "still_not_found"})
    devices = get_user_paired_devices(owner) if owner else []
    if not safe.startswith("seed_") and not any(safe.startswith(f"{d}_") for d in devices):
        raise HTTPException(status_code=403, detail={"code": "still_not_owned"})
    return FileResponse(str(path), media_type=media_files.media_type_for(path),
                        headers={"Cache-Control": "private, max-age=300"})
