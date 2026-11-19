"""ROBOT-STATE v2 HTTP (2026-11-19).

GET  /api/devices/{device_id}/state          -> the robot_state object (same as the E2 frame)
POST /api/devices/{device_id}/selected_dog   {"dog_id": "<uuid or null>"} -> 200 with the object

select_dog answers at once: selected_dog_id is stored (last write wins) with
selected_dog_applied=false, and select_dog goes to the robot in the background if it is online
(otherwise once on its next connect). State logic: app/services/robot_state.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import Actor, resolve_actor
from app.database import get_device_owner, get_dog_owner
from app.models import CommandRequest
from app.services import robot_control, robot_state

router = APIRouter(prefix="/api/devices", tags=["Robot state"])


class SelectedDogRequest(BaseModel):
    dog_id: Optional[str] = None


def _check_device(actor: Actor, device_id: str) -> str:
    """The robot's owner, if the actor may see (or change) this robot's state."""
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_not_allowed"})
    owner = get_device_owner(device_id)
    if not owner:
        raise HTTPException(status_code=404, detail={"code": "device_not_found"})
    if actor.kind != "admin" and actor.user_id != owner:
        raise HTTPException(status_code=404, detail={"code": "device_not_found"})
    return owner


@router.get("/{device_id}/state")
async def get_state(device_id: str, actor: Actor = Depends(resolve_actor)):
    _check_device(actor, device_id)
    return robot_state.public(device_id)


@router.post("/{device_id}/selected_dog")
async def post_selected_dog(device_id: str, body: SelectedDogRequest, actor: Actor = Depends(resolve_actor)):
    owner = _check_device(actor, device_id)
    dog_id = body.dog_id or None
    if dog_id is not None and get_dog_owner(dog_id) != owner:
        raise HTTPException(status_code=422, detail={"code": "unknown_dog", "dog_id": dog_id})
    return await robot_state.select_dog(device_id, dog_id, robot_state.source_for(actor.kind))


@router.post("/{device_id}/command", status_code=202)
async def post_device_command(device_id: str, body: CommandRequest, actor: Actor = Depends(resolve_actor)):
    """A command for one robot that names no dog: the voice agent's play_tts when no dog is selected."""
    _check_device(actor, device_id)
    return await robot_control.dispatch_device(device_id, body.command, body.data, actor)
