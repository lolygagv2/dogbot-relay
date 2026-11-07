"""Robot control route (Dec 9 demo build, 2026-11-06).

POST /api/dogs/{dog_id}/command  {command, data, device_id?}
POST /api/dogs/{dog_id}/speak    {text}   connector / agent only; WIM-Z voice only
Guards and the allowlist live in app/services/robot_control.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import Actor, check_dog_access, resolve_actor
from app.models import CommandRequest, SpeakRequest
from app.services import robot_control

router = APIRouter(tags=["Robot control"])


@router.post("/api/dogs/{dog_id}/command", status_code=202)
async def post_command(dog_id: str, body: CommandRequest, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "actor": "robot"})
    check_dog_access(actor, dog_id, write=True)
    result = await robot_control.dispatch(dog_id, body.command, body.data, actor, device_id=body.device_id,
                                          wait_s=body.wait_s)
    status = 200 if result.get("result") is not None else 202
    return JSONResponse(status_code=status, content=result)


@router.get("/api/dogs/{dog_id}/command/{command_id}")
async def get_command_result(dog_id: str, command_id: str, actor: Actor = Depends(resolve_actor)):
    """The robot's command_result for a relay-issued command (kept 10 minutes)."""
    from app.services import command_results
    check_dog_access(actor, dog_id)
    res = command_results.get(command_id)
    if not res or (res.get("dog_id") and res["dog_id"] != dog_id):
        raise HTTPException(status_code=404, detail={"code": "result_not_available"})
    return {k: res.get(k) for k in ("command_id", "command", "device_id", "ok", "result", "error", "timestamp")}


@router.post("/api/dogs/{dog_id}/speak", status_code=202)
async def post_speak(dog_id: str, body: SpeakRequest, actor: Actor = Depends(resolve_actor)):
    """An assistant makes the robot say `text` (<= 280 chars) in the WIM-Z voice."""
    if actor.kind not in robot_control.SPEAK_ACTORS:
        raise HTTPException(status_code=403, detail={"code": "speak_not_allowed", "actor": actor.kind})
    check_dog_access(actor, dog_id, write=True)
    return await robot_control.speak(dog_id, body.text, actor)
