"""Admin-only replayer controls (Dec 9 demo build, 2026-11-06; per-dog targets 2026-11-18).

Every call names its dog. One replay runs at a time; a call for a dog other than the loaded one
gets 409 replay_other_dog (with loaded_dog_id), except load and reset, which always work.

GET  /admin/replay?dog_id=     state for that dog (busy_with names another dog's replay)
GET  /admin/replay/seeds       available seed files
POST /admin/replay/load        {seed, dog_id, device_id?, date?}
POST /admin/replay/start       {dog_id, speed?}
POST /admin/replay/pause       {dog_id}
POST /admin/replay/speed       {dog_id, speed}
POST /admin/replay/jump        {dog_id, to: "HH:MM"} | {dog_id, offset_s}
POST /admin/replay/reset       {dog_id}  deletes that dog's replay-made data (only that)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.auth import Actor, require_admin
from app.models import ReplayJump, ReplayLoad, ReplaySpeed, ReplayStart, ReplayTarget
from app.services.replayer import Replayer, get_replayer

router = APIRouter(prefix="/admin/replay", tags=["Replay (admin)"])


@router.get("")
async def replay_state(dog_id: Optional[str] = Query(default=None), device_id: Optional[str] = Query(default=None),
                       _: Actor = Depends(require_admin)):
    return get_replayer().state_for(dog_id) if dog_id else get_replayer().state()


@router.get("/seeds")
async def replay_seeds(_: Actor = Depends(require_admin)):
    return {"seeds": Replayer.list_seeds()}


@router.post("/load")
async def replay_load(body: ReplayLoad, admin: Actor = Depends(require_admin)):
    return await get_replayer().load(body.seed, admin, dog_id=body.dog_id, device_id=body.device_id, date=body.date)


@router.post("/start")
async def replay_start(body: ReplayStart, _: Actor = Depends(require_admin)):
    return await get_replayer().start(body.speed, dog_id=body.dog_id)


@router.post("/pause")
async def replay_pause(body: ReplayTarget, _: Actor = Depends(require_admin)):
    return await get_replayer().pause(dog_id=body.dog_id)


@router.post("/speed")
async def replay_speed(body: ReplaySpeed, _: Actor = Depends(require_admin)):
    return await get_replayer().set_speed(body.speed, dog_id=body.dog_id)


@router.post("/jump")
async def replay_jump(body: ReplayJump, _: Actor = Depends(require_admin)):
    return await get_replayer().jump(offset_s=body.offset_s, to=body.to, dog_id=body.dog_id)


@router.post("/reset")
async def replay_reset(body: ReplayTarget, admin: Actor = Depends(require_admin)):
    return await get_replayer().reset_dog(body.dog_id, admin)
