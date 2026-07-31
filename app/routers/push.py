"""Push token registration endpoints (push contract 2026-07-30).

The app registers its FCM device token (+ per-type notification preferences)
on login, token rotation, and every preference edit — same endpoint, last
write wins. Unregister on logout is best-effort; stale tokens are also pruned
when FCM rejects them at send time.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.database import delete_push_token, upsert_push_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push", tags=["Push"])


class PushRegisterRequest(BaseModel):
    device_token: str = Field(..., min_length=1)
    platform: str = Field(..., pattern="^(ios|android)$")
    # [] is valid: master switch off — keep the row, send nothing. Unknown type
    # names are stored as-is; the sender just never matches them.
    enabled_types: list[str] = []


class PushUnregisterRequest(BaseModel):
    device_token: str = Field(..., min_length=1)


@router.post("/register")
async def register_push_token(
    body: PushRegisterRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    user_id = current_user["user_id"]
    row = upsert_push_token(
        device_token=body.device_token,
        user_id=user_id,
        platform=body.platform,
        enabled_types=body.enabled_types,
    )
    logger.info(
        f"[PUSH] Registered token ...{body.device_token[-8:]} user={user_id} "
        f"platform={body.platform} types={body.enabled_types}"
    )
    return {"status": "ok", "updated_at": row["updated_at"]}


@router.post("/unregister")
async def unregister_push_token(
    body: PushUnregisterRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    deleted = delete_push_token(body.device_token)
    logger.info(
        f"[PUSH] Unregistered token ...{body.device_token[-8:]} "
        f"user={current_user['user_id']} (existed={deleted})"
    )
    return {"status": "ok"}
