import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user
from app.connection_manager import ConnectionManager, get_connection_manager
from app.database import create_device_pairing, delete_device_pairing, get_device_owner as db_get_device_owner
from app.models import UserPairDeviceRequest, UserPairDeviceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["User"])


@router.post("/pair-device", response_model=UserPairDeviceResponse)
async def pair_device(
    request: UserPairDeviceRequest,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Pair a device with the current user by device_id.

    This allows the user to control the specified robot. The robot must
    connect to the relay with the same device_id for commands to route.
    Pairing is persisted to the database.
    """
    user_id = current_user["user_id"]
    device_id = request.device_id

    # Check if device is already owned by another user (check DB for persistence)
    current_owner = db_get_device_owner(device_id)
    if current_owner and current_owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device {device_id} is already paired with another user"
        )

    # Persist pairing to database
    create_device_pairing(user_id, device_id)

    # Update in-memory cache
    manager.set_device_owner(device_id, user_id)

    is_online = manager.is_robot_online(device_id)
    logger.info(f"User {user_id} paired with device {device_id} (online: {is_online}) - persisted to DB")

    return UserPairDeviceResponse(
        success=True,
        device_id=device_id,
        message=f"Paired with {device_id}" + (" (online)" if is_online else " (offline - will connect when robot comes online)")
    )


@router.post("/unpair-device", response_model=UserPairDeviceResponse)
async def unpair_device(
    request: UserPairDeviceRequest,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Unpair a device from the current user.

    After unpairing, commands will no longer route to this device.
    Unpairing is persisted to the database.
    """
    user_id = current_user["user_id"]
    device_id = request.device_id

    # Check ownership from DB
    current_owner = db_get_device_owner(device_id)
    if not current_owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} is not paired with any user"
        )

    if current_owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to unpair this device"
        )

    # Remove pairing from database
    delete_device_pairing(device_id)

    # Update in-memory cache
    manager.remove_device_owner(device_id)
    logger.info(f"User {user_id} unpaired device {device_id} - removed from DB")

    return UserPairDeviceResponse(
        success=True,
        device_id=device_id,
        message=f"Unpaired from {device_id}"
    )


@router.get("/devices")
async def get_user_devices(
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Get all devices paired with the current user.
    """
    user_id = current_user["user_id"]
    device_ids = manager.get_user_devices(user_id)

    devices = []
    for device_id in device_ids:
        devices.append({
            "device_id": device_id,
            "is_online": manager.is_robot_online(device_id)
        })

    return {"devices": devices}
