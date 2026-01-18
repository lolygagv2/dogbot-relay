import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services.turn_service import turn_service
from app.auth import get_current_user

router = APIRouter(prefix="/api/turn", tags=["TURN"])


class CredentialRequest(BaseModel):
    ttl: Optional[int] = 3600


@router.post("/credentials")
async def generate_turn_credentials(
    request: CredentialRequest,
    user: dict = Depends(get_current_user)
):
    """Generate short-lived TURN credentials for WebRTC."""
    try:
        credentials = await turn_service.generate_credentials(request.ttl)
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
