"""Shared settings + sound library routes (Dec 9 demo build, 2026-11-06).

GET  /api/dogs/{dog_id}/settings                 all keys (defaults materialised)
GET  /api/dogs/{dog_id}/settings/audit
GET  /api/dogs/{dog_id}/settings/{key}
PUT  /api/dogs/{dog_id}/settings/{key}           {value, expected_version?}  (guards live in the service)
POST /api/dogs/{dog_id}/settings/{key}/reset
GET  /api/dogs/{dog_id}/sounds                   metadata only
POST /api/dogs/{dog_id}/sounds/{sound_id}        app/admin: replace a sound (multipart)
POST /api/dogs/{dog_id}/sounds/reset
GET  /api/sounds/file/{dog_id}/{sound_id}        robot (HMAC) or app (JWT) only
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.auth import Actor, check_dog_access, resolve_actor
from app.config import get_settings
from app.database import get_user_paired_devices, list_settings_audit
from app.models import SettingPut, SoundDef, Sounds
from app.services import settings_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Settings"])

SOUND_EXTS = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
MAX_SOUND_BYTES = 5 * 1024 * 1024
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _sounds_dir() -> Path:
    s = get_settings()
    base = Path(s.sounds_storage_dir) if s.sounds_storage_dir else Path(__file__).parent.parent.parent / "data" / "sounds"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _row_out(row: dict) -> dict:
    return {k: row.get(k) for k in ("key", "value", "version", "updated_at", "updated_by")}


# ---- settings ----

@router.get("/api/dogs/{dog_id}/settings")
async def get_settings_all(dog_id: str, actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    rows = settings_service.get_all_settings(dog_id)
    return {"dog_id": dog_id, "settings": {k: _row_out(v) for k, v in rows.items()}}


@router.get("/api/dogs/{dog_id}/settings/audit")
async def get_settings_audit(dog_id: str, limit: int = Query(default=50, ge=1, le=500),
                             actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    if actor.kind not in ("app", "admin"):
        raise HTTPException(status_code=403, detail={"code": "app_only"})
    return {"dog_id": dog_id, "entries": list_settings_audit(dog_id, limit)}


@router.get("/api/dogs/{dog_id}/settings/{key}")
async def get_setting(dog_id: str, key: str, actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    return _row_out(settings_service.get_setting(dog_id, key))


@router.put("/api/dogs/{dog_id}/settings/{key}")
async def put_setting(dog_id: str, key: str, body: SettingPut, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_read_only"})
    check_dog_access(actor, dog_id, write=True)
    row = await settings_service.put_setting(dog_id, key, body.value, actor, body.expected_version)
    return _row_out(row)


@router.post("/api/dogs/{dog_id}/settings/{key}/reset")
async def reset_setting(dog_id: str, key: str, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_read_only"})
    check_dog_access(actor, dog_id, write=True)
    row = await settings_service.reset_setting(dog_id, key, actor)
    return _row_out(row)


# ---- sounds ----

@router.get("/api/dogs/{dog_id}/sounds")
async def get_sounds(dog_id: str, actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    return settings_service.get_value(dog_id, "sounds")


@router.post("/api/dogs/{dog_id}/sounds/reset")
async def reset_sounds(dog_id: str, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_read_only"})
    check_dog_access(actor, dog_id, write=True)
    row = await settings_service.reset_setting(dog_id, "sounds", actor)
    # remove custom files
    d = _sounds_dir() / dog_id
    if d.exists():
        for f in d.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
    return row["value"]


@router.post("/api/dogs/{dog_id}/sounds/{sound_id}")
async def replace_sound(dog_id: str, sound_id: str, file: UploadFile = File(...),
                        label: str = Query(default=""), actor: Actor = Depends(resolve_actor)):
    if actor.kind not in ("app", "admin"):
        raise HTTPException(status_code=403, detail={"code": "app_only", "reason": "sound upload is an owner action"})
    check_dog_access(actor, dog_id, write=True)
    if not _ID_RE.match(sound_id):
        raise HTTPException(status_code=422, detail={"code": "bad_sound_id"})
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SOUND_EXTS:
        raise HTTPException(status_code=422, detail={"code": "bad_format", "allowed": list(SOUND_EXTS)})
    content = await file.read()
    if len(content) > MAX_SOUND_BYTES:
        raise HTTPException(status_code=413, detail={"code": "too_large", "max_bytes": MAX_SOUND_BYTES})
    d = _sounds_dir() / dog_id
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{sound_id}.*"):
        old.unlink(missing_ok=True)
    (d / f"{sound_id}{ext}").write_bytes(content)

    sounds = Sounds.model_validate(settings_service.get_value(dog_id, "sounds"))
    url = settings_service.sound_url(dog_id, sound_id)
    entry = next((s for s in sounds.sounds if s.id == sound_id), None)
    if entry:
        entry.builtin, entry.file, entry.url = False, f"{sound_id}{ext}", url
        if label:
            entry.label = label
    else:
        sounds.sounds.append(SoundDef(
            id=sound_id, label=label or sound_id, builtin=False, file=f"{sound_id}{ext}", url=url))
    row = await settings_service.put_setting(dog_id, "sounds", sounds.model_dump(), actor)

    # robot prefetch hint (like voice_command_updated)
    try:
        from app.connection_manager import get_connection_manager
        from app.database import get_dog_owner
        manager = get_connection_manager()
        owner = get_dog_owner(dog_id)
        for dev in (get_user_paired_devices(owner) if owner else []):
            await manager.send_to_robot(dev, {"type": "sound_updated", "dog_id": dog_id, "sound_id": sound_id,
                                              "url": url, "file": f"{sound_id}{ext}"})
    except Exception as e:  # pragma: no cover
        logger.warning(f"[SOUNDS] robot notify failed: {e}")
    return row["value"]


@router.get("/api/sounds/file/{dog_id}/{sound_id}")
async def get_sound_file(dog_id: str, sound_id: str, actor: Actor = Depends(resolve_actor)):
    # Never served to connector/agent: metadata only for them.
    if actor.kind not in ("robot", "app", "admin"):
        raise HTTPException(status_code=403, detail={"code": "robot_or_app_only"})
    check_dog_access(actor, dog_id)
    if not _ID_RE.match(sound_id):
        raise HTTPException(status_code=422, detail={"code": "bad_sound_id"})
    d = _sounds_dir() / dog_id
    for ext, media in SOUND_EXTS.items():
        f = d / f"{sound_id}{ext}"
        if f.exists():
            return FileResponse(str(f), media_type=media, filename=f.name)
    # builtin sounds live on the robot; the relay has no bytes for them
    sounds = Sounds.model_validate(settings_service.get_value(dog_id, "sounds"))
    if any(s.id == sound_id and s.builtin for s in sounds.sounds):
        raise HTTPException(status_code=404, detail={"code": "builtin_sound_on_robot", "sound_id": sound_id})
    raise HTTPException(status_code=404, detail={"code": "sound_not_found", "sound_id": sound_id})
