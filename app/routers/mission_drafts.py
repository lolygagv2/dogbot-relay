"""Mission drafts: assistants propose, the owner approves in the app (amendment 2026-11-06).

POST /api/dogs/{dog_id}/missions/drafts              connector/agent/app: create a pending draft (+ WS mission_draft)
GET  /api/dogs/{dog_id}/missions/drafts?status=      list
GET  /api/dogs/{dog_id}/missions/drafts/{draft_id}   one (assistants check approval here)
PUT  /api/dogs/{dog_id}/missions/drafts/{draft_id}   app owner/caretaker: {"decision": "approve"|"decline"}
                                                     approve appends the mission to the `missions` setting
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import Actor, check_dog_access, resolve_actor
from app.database import (
    decide_mission_draft,
    get_mission_draft,
    insert_mission_draft,
    list_mission_drafts,
)
from app.models import Missions, MissionDraftCreate, MissionDraftDecision
from app.services import settings_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dogs/{dog_id}/missions/drafts", tags=["Mission drafts"])


def _public(d: dict) -> dict:
    return {k: d.get(k) for k in ("draft_id", "dog_id", "actor", "status", "draft", "warnings", "note",
                                  "mission_id", "created_at", "decided_at")}


@router.post("", status_code=201)
async def create_draft(dog_id: str, body: MissionDraftCreate, actor: Actor = Depends(resolve_actor)):
    if actor.kind == "robot":
        raise HTTPException(status_code=403, detail={"code": "robot_read_only"})
    owner = check_dog_access(actor, dog_id, write=True)
    draft = body.draft.model_dump()
    if draft["trigger"]["kind"] == "schedule" and not draft["trigger"].get("time"):
        raise HTTPException(status_code=422, detail={"code": "invalid_value", "reason": "scheduled mission needs trigger.time"})
    draft_id = "d_" + secrets.token_urlsafe(9)
    row = insert_mission_draft(draft_id, dog_id, owner or actor.user_id, actor.label, draft, body.warnings, body.note)
    try:
        from app.connection_manager import get_connection_manager
        await get_connection_manager().send_to_user_apps(owner or actor.user_id, {
            "type": "mission_draft", "dog_id": dog_id, "draft_id": draft_id, "draft": draft,
            "warnings": body.warnings, "actor": actor.label, "created_at": row["created_at"],
        })
    except Exception as e:  # pragma: no cover
        logger.warning(f"[DRAFT] app notify failed: {e}")
    logger.info(f"[DRAFT] {actor.label} proposed {draft['type']} for {dog_id}: {draft_id}")
    return _public(row)


@router.get("")
async def list_drafts(dog_id: str, status: str = Query(default="pending"), limit: int = Query(default=50, ge=1, le=200),
                      actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    return {"dog_id": dog_id, "drafts": [_public(d) for d in list_mission_drafts(dog_id, status, limit)]}


@router.get("/{draft_id}")
async def get_draft(dog_id: str, draft_id: str, actor: Actor = Depends(resolve_actor)):
    check_dog_access(actor, dog_id)
    d = get_mission_draft(draft_id)
    if not d or d["dog_id"] != dog_id:
        raise HTTPException(status_code=404, detail={"code": "draft_not_found"})
    return _public(d)


@router.put("/{draft_id}")
async def decide_draft(dog_id: str, draft_id: str, body: MissionDraftDecision, actor: Actor = Depends(resolve_actor)):
    if actor.kind not in ("app", "admin"):
        raise HTTPException(status_code=403, detail={"code": "owner_decision_only",
                                                     "reason": "mission drafts are approved in the WIM-Z app"})
    check_dog_access(actor, dog_id, write=True)
    d = get_mission_draft(draft_id)
    if not d or d["dog_id"] != dog_id:
        raise HTTPException(status_code=404, detail={"code": "draft_not_found"})
    if d["status"] != "pending":
        raise HTTPException(status_code=409, detail={"code": "draft_already_decided", "status": d["status"]})

    if body.decision == "decline":
        decide_mission_draft(draft_id, "declined", actor.label)
        return {"draft_id": draft_id, "status": "declined"}

    mission = dict(d["draft"])
    missions = Missions.model_validate(settings_service.get_value(dog_id, "missions")).model_dump()
    if any(m["id"] == mission["id"] for m in missions["missions"]):
        mission["id"] = f"{mission['id']}_{secrets.token_hex(2)}"
    missions["missions"].append(mission)
    await settings_service.put_setting(dog_id, "missions", missions, actor)  # app actor: unrestricted
    decide_mission_draft(draft_id, "approved", actor.label, mission["id"])
    logger.info(f"[DRAFT] {actor.label} approved {draft_id} -> mission {mission['id']} for {dog_id}")
    return {"draft_id": draft_id, "status": "approved", "mission_id": mission["id"],
            "decided_at": datetime.now(timezone.utc).isoformat()}
