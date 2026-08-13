import logging
import secrets

from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["Config"])


@router.get("/anthropic-key")
async def get_anthropic_key(authorization: str = Header(default="")):
    """Serve the Anthropic API key to authorized miniapp clients (Tripwire).

    Auth is a static bearer token (miniapp_config_token). Both the token and
    the key live only in the server's .env — the miniapp bundle ships the
    token, never the key, so rotating the key is a server-side change with no
    app rebuild.
    """
    settings = get_settings()
    if not settings.miniapp_config_token or not settings.anthropic_api_key:
        raise HTTPException(status_code=404, detail="Not configured")

    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.miniapp_config_token):
        logger.warning("[CONFIG] anthropic-key request with invalid token")
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"api_key": settings.anthropic_api_key}
