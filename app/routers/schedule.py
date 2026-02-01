"""
Mission Schedule Router (Build 34, Updated Build 35, DEPRECATED Build 38)

REST endpoints for managing mission schedules.
Supports both relay format and app format field names.

App calls: /schedules (with schedule_id, mission_name, start_time, days_of_week)
Relay also supports: /missions/schedule (with id, mission_id, hour, minute, weekdays)

DEPRECATION NOTICE (Build 38):
    Schedules now live on the ROBOT, not the relay.
    These endpoints are deprecated and will be removed in a future build.
    Apps should send schedule commands via WebSocket directly to the robot.
    The relay will forward schedule_* commands to the robot.
"""
import logging
import uuid
import warnings

from fastapi import APIRouter, Depends, HTTPException, Request, status

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
    DAY_NAME_TO_NUM,
    Schedule,
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleUpdate,
    SuccessResponse,
)

logger = logging.getLogger(__name__)

# Main router at /schedules (what app calls)
router = APIRouter(tags=["Schedules (Deprecated)"])


def log_deprecation_warning(endpoint: str, user_id: str):
    """Log a deprecation warning for schedule endpoints."""
    logger.warning(
        f"[DEPRECATED] Schedule endpoint '{endpoint}' called by user {user_id}. "
        f"Schedules now live on the robot. Use WebSocket commands instead."
    )


def parse_end_time(end_time: str) -> tuple[int, int]:
    """Parse end_time string to hour and minute."""
    if not end_time:
        return None, None
    try:
        parts = end_time.split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None


@router.post("/schedules", response_model=Schedule)
@router.post("/missions/schedule", response_model=Schedule)
async def create_mission_schedule(
    schedule_data: ScheduleCreate,
    user: dict = Depends(get_current_user)
):
    """Create a new mission schedule. Accepts both app and relay field formats.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning("POST /schedules", user_id)
    # Use helper methods to get values from either format
    schedule_id = schedule_data.get_schedule_id() or str(uuid.uuid4())
    mission_id = schedule_data.get_mission_id()
    hour = schedule_data.get_hour()
    minute = schedule_data.get_minute()
    weekdays = schedule_data.get_weekdays()

    # Parse end_time if provided
    end_hour, end_minute = parse_end_time(schedule_data.end_time)

    if not mission_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mission_name or mission_id is required"
        )

    try:
        result = create_schedule(
            schedule_id=schedule_id,
            user_id=user_id,
            dog_id=schedule_data.dog_id,
            mission_id=mission_id,
            schedule_type=schedule_data.type.value,
            hour=hour,
            minute=minute,
            end_hour=end_hour,
            end_minute=end_minute,
            weekdays=weekdays,
            cooldown_hours=schedule_data.cooldown_hours,
            name=schedule_data.name,
            enabled=schedule_data.enabled,
        )
        logger.info(f"[SCHEDULE] Created schedule {schedule_id} for user {user_id}, mission {mission_id}")
        return result
    except Exception as e:
        logger.error(f"[SCHEDULE] Failed to create schedule: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/schedules", response_model=ScheduleListResponse)
@router.get("/missions/schedule", response_model=ScheduleListResponse)
async def list_schedules(user: dict = Depends(get_current_user)):
    """List all schedules for the current user.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning("GET /schedules", user_id)
    schedules = get_user_schedules(user_id)
    scheduling_enabled = get_scheduling_enabled(user_id)

    return ScheduleListResponse(
        schedules=schedules,
        scheduling_enabled=scheduling_enabled
    )


@router.get("/schedules/{schedule_id}", response_model=Schedule)
@router.get("/missions/schedule/{schedule_id}", response_model=Schedule)
async def get_schedule(
    schedule_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific schedule by ID.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning(f"GET /schedules/{schedule_id}", user_id)
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


@router.put("/schedules/{schedule_id}", response_model=Schedule)
@router.put("/missions/schedule/{schedule_id}", response_model=Schedule)
async def update_mission_schedule(
    schedule_id: str,
    schedule_data: ScheduleUpdate,
    user: dict = Depends(get_current_user)
):
    """Update an existing schedule. Accepts both app and relay field formats.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning(f"PUT /schedules/{schedule_id}", user_id)
    update_fields = {}

    # Handle mission_id from either format
    if schedule_data.mission_name is not None:
        update_fields["mission_id"] = schedule_data.mission_name
    elif schedule_data.mission_id is not None:
        update_fields["mission_id"] = schedule_data.mission_id

    if schedule_data.dog_id is not None:
        update_fields["dog_id"] = schedule_data.dog_id
    if schedule_data.name is not None:
        update_fields["name"] = schedule_data.name
    if schedule_data.type is not None:
        update_fields["type"] = schedule_data.type.value

    # Handle time from either format
    if schedule_data.start_time is not None:
        try:
            parts = schedule_data.start_time.split(":")
            update_fields["hour"] = int(parts[0])
            update_fields["minute"] = int(parts[1])
        except (ValueError, IndexError):
            pass
    else:
        if schedule_data.hour is not None:
            update_fields["hour"] = schedule_data.hour
        if schedule_data.minute is not None:
            update_fields["minute"] = schedule_data.minute

    if schedule_data.end_time is not None:
        end_hour, end_minute = parse_end_time(schedule_data.end_time)
        if end_hour is not None:
            update_fields["end_hour"] = end_hour
            update_fields["end_minute"] = end_minute

    # Handle weekdays from either format
    if schedule_data.days_of_week is not None:
        update_fields["weekdays"] = [
            DAY_NAME_TO_NUM.get(d.lower(), 0)
            for d in schedule_data.days_of_week
            if d.lower() in DAY_NAME_TO_NUM
        ]
    elif schedule_data.weekdays is not None:
        update_fields["weekdays"] = schedule_data.weekdays

    if schedule_data.cooldown_hours is not None:
        update_fields["cooldown_hours"] = schedule_data.cooldown_hours
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


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/missions/schedule/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mission_schedule(
    schedule_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a schedule.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning(f"DELETE /schedules/{schedule_id}", user_id)
    deleted = delete_schedule(schedule_id, user_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found or not authorized"
        )

    logger.info(f"[SCHEDULE] Deleted schedule {schedule_id}")
    return None


@router.post("/schedules/enable", response_model=SuccessResponse)
@router.post("/missions/schedule/enable", response_model=SuccessResponse)
async def enable_scheduling(user: dict = Depends(get_current_user)):
    """Enable global scheduling for the current user.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning("POST /schedules/enable", user_id)
    set_scheduling_enabled(user_id, True)
    logger.info(f"[SCHEDULE] Enabled scheduling for user {user_id}")
    return SuccessResponse(success=True)


@router.post("/schedules/disable", response_model=SuccessResponse)
@router.post("/missions/schedule/disable", response_model=SuccessResponse)
async def disable_scheduling(user: dict = Depends(get_current_user)):
    """Disable global scheduling for the current user.

    DEPRECATED: Schedules now live on the robot. Use WebSocket commands instead.
    """
    user_id = user.get("user_id")
    log_deprecation_warning("POST /schedules/disable", user_id)
    set_scheduling_enabled(user_id, False)
    logger.info(f"[SCHEDULE] Disabled scheduling for user {user_id}")
    return SuccessResponse(success=True)
