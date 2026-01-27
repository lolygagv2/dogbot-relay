"""Dog metrics endpoints for tracking treats, detections, missions, and session time."""

import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user
from app.database import (
    get_metric_history,
    get_metrics,
    get_user_dog_role,
    log_metric,
    log_mission,
)
from app.models import MetricEventRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metrics", tags=["Metrics"])


@router.post("/log")
async def log_metric_event(
    request: MetricEventRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Log a metric event for a dog. Requires user to have access to the dog."""
    user_id = current_user["user_id"]

    # Verify user has access to this dog
    role = get_user_dog_role(user_id, request.dog_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this dog",
        )

    # If mission data is provided, log as mission
    if request.mission_type and request.mission_result:
        result = log_mission(
            dog_id=request.dog_id,
            user_id=user_id,
            mission_type=request.mission_type,
            result=request.mission_result,
            details=request.details,
        )
        return {"success": True, "logged": "mission", **result}

    # Otherwise log as a simple metric increment
    try:
        result = log_metric(
            dog_id=request.dog_id,
            user_id=user_id,
            metric_type=request.metric_type,
            value=request.value,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return {"success": True, "logged": "metric", **result}


@router.get("/{dog_id}")
async def get_dog_metrics(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    period: str = Query("daily", pattern="^(daily|weekly|lifetime)$"),
):
    """Get aggregated metrics for a dog. Period: daily, weekly, or lifetime."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this dog",
        )

    since_date = None
    if period == "daily":
        since_date = date.today().isoformat()
    elif period == "weekly":
        since_date = (date.today() - timedelta(days=7)).isoformat()
    # lifetime: since_date stays None -> no date filter

    metrics = get_metrics(dog_id, user_id, since_date)
    metrics["period"] = period
    return metrics


@router.get("/{dog_id}/history")
async def get_dog_metric_history(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    days: int = Query(7, ge=1, le=365),
):
    """Get daily metric breakdown for charts. Returns one row per day."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this dog",
        )

    history = get_metric_history(dog_id, user_id, days)
    return {"dog_id": dog_id, "days": days, "history": history}
