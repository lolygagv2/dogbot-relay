"""Admin-only replayer controls (Dec 9 demo build, 2026-11-06).

GET  /admin/replay            state
GET  /admin/replay/seeds      available seed files
POST /admin/replay/load       {seed, dog_id?, device_id?, date?}
POST /admin/replay/start      {speed?}
POST /admin/replay/pause
POST /admin/replay/speed      {speed}
POST /admin/replay/jump       {to: "HH:MM"} | {offset_s}
POST /admin/replay/reset      deletes the demo dog's replay data
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends

from app.auth import Actor, require_admin
from app.models import ReplayJump, ReplayLoad, ReplaySpeed
from app.services.replayer import Replayer, get_replayer

router = APIRouter(prefix="/admin/replay", tags=["Replay (admin)"])


@router.get("")
async def replay_state(_: Actor = Depends(require_admin)):
    return get_replayer().state()


@router.get("/seeds")
async def replay_seeds(_: Actor = Depends(require_admin)):
    return {"seeds": Replayer.list_seeds()}


@router.post("/load")
async def replay_load(body: ReplayLoad, admin: Actor = Depends(require_admin)):
    return await get_replayer().load(body.seed, admin, dog_id=body.dog_id, device_id=body.device_id, date=body.date)


@router.post("/start")
async def replay_start(body: Optional[ReplaySpeed] = Body(default=None), _: Actor = Depends(require_admin)):
    return await get_replayer().start(body.speed if body else None)


@router.post("/pause")
async def replay_pause(_: Actor = Depends(require_admin)):
    return await get_replayer().pause()


@router.post("/speed")
async def replay_speed(body: ReplaySpeed, _: Actor = Depends(require_admin)):
    return await get_replayer().set_speed(body.speed)


@router.post("/jump")
async def replay_jump(body: ReplayJump, _: Actor = Depends(require_admin)):
    return await get_replayer().jump(offset_s=body.offset_s, to=body.to)


@router.post("/reset")
async def replay_reset(_: Actor = Depends(require_admin)):
    return await get_replayer().reset()
