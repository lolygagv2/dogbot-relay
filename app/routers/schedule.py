"""
Mission Schedule Router (Build 34)

REST endpoints for managing mission schedules.
Schedules are stored in the relay database and can trigger missions on connected robots.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user
from app.database import (
    create_schedule,
    delete_schedule,
    get_schedule_by_id,
    get_scheduling_enabled,
    get_user_schedules,
    set_scheduling_enabled,
    update_schedule,
)
from app.models import (
    Schedule,
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleUpdate,
    SuccessResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/missions/schedule", tags=["Schedules"])


@router.post("", response_model=Schedule)
async def create_mission_schedule(
    schedule_data: ScheduleCreate,
    user_id: str = Depends(get_current_user)
):
    """Create a new mission schedule."""
    # Generate ID if not provided
    schedule_id = schedule_data.id or str(uuid.uuid4())

    try:
        result = create_schedule(
            schedule_id=schedule_id,
            user_id=user_id,
            dog_id=schedule_data.dog_id,
            mission_id=schedule_data.mission_id,
            schedule_type=schedule_data.type.value,
            hour=schedule_data.hour,
            minute=schedule_data.minute,
            weekdays=schedule_data.weekdays,
            name=schedule_data.name,
            enabled=schedule_data.enabled,
        )
        logger.info(f"[SCHEDULE] Created schedule {schedule_id} for user {user_id}")
        return result
    except Exception as e:
        logger.error(f"[SCHEDULE] Failed to create schedule: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(user_id: str = Depends(get_current_user)):
    """List all schedules for the current user."""
    schedules = get_user_schedules(user_id)
    scheduling_enabled = get_scheduling_enabled(user_id)

    return ScheduleListResponse(
        schedules=schedules,
        scheduling_enabled=scheduling_enabled
    )


@router.get("/{schedule_id}", response_model=Schedule)
async def get_schedule(
    schedule_id: str,
    user_id: str = Depends(get_current_user)
):
    """Get a specific schedule by ID."""
    schedule = get_schedule_by_id(schedule_id)

    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    if schedule["user_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this schedule"
        )

    return schedule


@router.put("/{schedule_id}", response_model=Schedule)
async def update_mission_schedule(
    schedule_id: str,
    schedule_data: ScheduleUpdate,
    user_id: str = Depends(get_current_user)
):
    """Update an existing schedule."""
    # Build update fields from non-None values
    update_fields = {}
    if schedule_data.mission_id is not None:
        update_fields["mission_id"] = schedule_data.mission_id
    if schedule_data.dog_id is not None:
        update_fields["dog_id"] = schedule_data.dog_id
    if schedule_data.name is not None:
        update_fields["name"] = schedule_data.name
    if schedule_data.type is not None:
        update_fields["type"] = schedule_data.type.value
    if schedule_data.hour is not None:
        update_fields["hour"] = schedule_data.hour
    if schedule_data.minute is not None:
        update_fields["minute"] = schedule_data.minute
    if schedule_data.weekdays is not None:
        update_fields["weekdays"] = schedule_data.weekdays
    if schedule_data.enabled is not None:
        update_fields["enabled"] = schedule_data.enabled

    result = update_schedule(schedule_id, user_id, **update_fields)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found or not authorized"
        )

    logger.info(f"[SCHEDULE] Updated schedule {schedule_id}")
    return result


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mission_schedule(
    schedule_id: str,
    user_id: str = Depends(get_current_user)
):
    """Delete a schedule."""
    deleted = delete_schedule(schedule_id, user_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found or not authorized"
        )

    logger.info(f"[SCHEDULE] Deleted schedule {schedule_id}")
    return None


@router.post("/enable", response_model=SuccessResponse)
async def enable_scheduling(user_id: str = Depends(get_current_user)):
    """Enable global scheduling for the current user."""
    set_scheduling_enabled(user_id, True)
    logger.info(f"[SCHEDULE] Enabled scheduling for user {user_id}")
    return SuccessResponse(success=True)


@router.post("/disable", response_model=SuccessResponse)
async def disable_scheduling(user_id: str = Depends(get_current_user)):
    """Disable global scheduling for the current user."""
    set_scheduling_enabled(user_id, False)
    logger.info(f"[SCHEDULE] Disabled scheduling for user {user_id}")
    return SuccessResponse(success=True)
