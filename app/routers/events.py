"""Device event history endpoints for offline event retrieval (Build 46)."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user
from app.database import get_device_events, get_device_event_summary, get_device_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["Events"])


@router.get("/{device_id}")
async def get_events(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    start: Optional[str] = Query(None, description="Start time (ISO 8601)"),
    end: Optional[str] = Query(None, description="End time (ISO 8601)"),
    type: Optional[str] = Query(None, description="Filter by event type"),
    dog_id: Optional[str] = Query(None, description="Filter by dog ID"),
    limit: int = Query(50, ge=1, le=200, description="Max events to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """
    Get stored events for a device. Requires JWT auth.
    The authenticated user must be the device owner.
    """
    user_id = current_user["user_id"]

    owner_id = get_device_owner(device_id)
    if owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this device's events",
        )

    return get_device_events(
        device_id=device_id,
        owner_user_id=user_id,
        start=start,
        end=end,
        event_type=type,
        dog_id=dog_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{device_id}/summary")
async def get_event_summary(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    period: str = Query("7d", description="Period: 7d or 30d"),
):
    """
    Get aggregated event summary for the app dashboard.
    Returns daily scores, total treats/tricks/barks, and active minutes.
    """
    user_id = current_user["user_id"]

    owner_id = get_device_owner(device_id)
    if owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this device's events",
        )

    days = 30 if period == "30d" else 7

    return get_device_event_summary(
        device_id=device_id,
        owner_user_id=user_id,
        days=days,
    )
