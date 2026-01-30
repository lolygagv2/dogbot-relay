import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from app.services.turn_service import turn_service
from app.auth import get_current_user
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/turn", tags=["TURN"])


class CredentialRequest(BaseModel):
    ttl: Optional[int] = None  # Will use config default if not specified


@router.post("/credentials")
async def generate_turn_credentials(
    request: CredentialRequest,
    user: dict = Depends(get_current_user)
):
    """Generate TURN credentials for WebRTC with 24-hour TTL by default."""
    settings = get_settings()
    ttl = request.ttl if request.ttl is not None else settings.turn_credential_ttl

    try:
        credentials = await turn_service.generate_credentials(ttl)
        logger.info(f"[TURN] Generated credentials for user {user.get('sub', 'unknown')}, TTL={ttl}s")
        return credentials
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Cloudflare API error: {e.response.status_code}"
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TURN service error: {str(e)}")


@router.get("/credentials")
async def get_turn_credentials(
    device_id: str = Query(..., description="Device ID requesting credentials"),
    user: dict = Depends(get_current_user)
):
    """
    Get fresh TURN credentials for WebRTC.
    Call this before starting a new WebRTC session.
    """
    settings = get_settings()
    ttl = settings.turn_credential_ttl

    try:
        credentials = await turn_service.generate_credentials(ttl)
        logger.info(f"[TURN] Generated credentials for device {device_id} (user {user.get('sub', 'unknown')}), expires in {ttl}s")
        return credentials
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Cloudflare API error: {e.response.status_code}"
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TURN service error: {str(e)}")
