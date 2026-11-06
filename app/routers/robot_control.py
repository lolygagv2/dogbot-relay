"""Robot control route (Dec 9 demo build, 2026-11-06).

POST /api/dogs/{dog_id}/command  {command, data, device_id?}
Guards and the allowlist live in app/services/robot_control.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import Actor, check_dog_access, resolve_actor
from app.models import CommandRequest
from app.services import robot_control

router = APIRouter(tags=["Robot control"])


@router.post("/api/dogs/{dog_id}/command", status_code=202)
async def post_command(dog_id: str, body: CommandRequest, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "actor": "robot"})
    check_dog_access(actor, dog_id, write=True)
    result = await robot_control.dispatch(dog_id, body.command, body.data, actor, device_id=body.device_id)
    return JSONResponse(status_code=202, content=result)
