"""AI connectors + voice-agent transcript routes for the app (amendment 2026-11-06).

GET  /api/dogs/{dog_id}/ai/connectors                     -> {connector_url, assistants[]}
POST /api/dogs/{dog_id}/ai/connectors/{client_id}/revoke
POST /api/dogs/{dog_id}/ai/revoke_all
GET  /api/dogs/{dog_id}/ai/audit?limit=50
POST /api/dogs/{dog_id}/agent/transcript                  agent (internal) -> WS agent_transcript to the owner's apps

App JWT, owner or caretaker of the dog (viewers get 403 owner_or_caretaker_only).
Reads the shared oauth_clients / oauth_tokens / mcp_audit tables written by the MCP and
agent processes (same SQLite file).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import Actor, check_dog_access, resolve_actor
from app.config import get_settings
from app.mcp_server import oauth_db
from app.models import AgentTranscriptIn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["AI connectors"])


def _app_only(actor: Actor) -> None:
    if actor.kind not in ("app", "admin"):
        raise HTTPException(status_code=403, detail={"code": "app_only"})


def _owner_or_caretaker(actor: Actor, dog_id: str) -> None:
    """App JWT for the dog's owner or caretaker (viewers may not see or manage AI connectors)."""
    from app.database import get_user_dog_role
    _app_only(actor)
    check_dog_access(actor, dog_id)
    if actor.kind == "app" and get_user_dog_role(actor.user_id, dog_id) not in ("owner", "caretaker"):
        raise HTTPException(status_code=403, detail={"code": "owner_or_caretaker_only"})


@router.get("/api/dogs/{dog_id}/ai/connectors")
async def list_connectors(dog_id: str, actor: Actor = Depends(resolve_actor)):
    _owner_or_caretaker(actor, dog_id)
    s = get_settings()
    base = (s.mcp_public_base_url or s.public_base_url).rstrip("/")
    return {"connector_url": f"{base}/mcp", "assistants": oauth_db.list_connectors_for_user(actor.user_id)}


@router.post("/api/dogs/{dog_id}/ai/connectors/{client_id}/revoke")
async def revoke_connector(dog_id: str, client_id: str, actor: Actor = Depends(resolve_actor)):
    _owner_or_caretaker(actor, dog_id)
    n = oauth_db.revoke_client_for_user(client_id, actor.user_id)
    logger.info(f"[AI] {actor.label} revoked connector {client_id}: {n} token(s)")
    return {"client_id": client_id, "revoked": n}


@router.post("/api/dogs/{dog_id}/ai/revoke_all")
async def revoke_all(dog_id: str, actor: Actor = Depends(resolve_actor)):
    _owner_or_caretaker(actor, dog_id)
    n = oauth_db.revoke_all_for_user(actor.user_id)
    logger.info(f"[AI] {actor.label} revoked all connectors: {n} token(s)")
    return {"revoked": n}


@router.get("/api/dogs/{dog_id}/ai/audit")
async def ai_audit(dog_id: str, limit: int = Query(default=50, ge=1, le=500), actor: Actor = Depends(resolve_actor)):
    _owner_or_caretaker(actor, dog_id)
    return {"dog_id": dog_id, "entries": oauth_db.list_audit_for_user(actor.user_id, dog_id, limit)}


@router.post("/api/dogs/{dog_id}/agent/transcript", status_code=202)
async def agent_transcript(dog_id: str, body: AgentTranscriptIn, actor: Actor = Depends(resolve_actor)):
    if actor.kind not in ("agent", "admin"):
        raise HTTPException(status_code=403, detail={"code": "agent_only"})
    owner = check_dog_access(actor, dog_id)
    from app.connection_manager import get_connection_manager
    sent = await get_connection_manager().send_to_user_apps(owner or actor.user_id, {
        "type": "agent_transcript", "dog_id": dog_id, "role": body.role, "text": body.text,
        "ts": body.ts or datetime.now(timezone.utc).isoformat(),
    })
    return {"forwarded_to": sent}
