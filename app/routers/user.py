import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user
from app.connection_manager import ConnectionManager, get_connection_manager
from app.database import (
    create_device_pairing,
    delete_device_pairing,
    get_device_owner as db_get_device_owner,
    get_dog_by_id,
)
from app.models import UserPairDeviceRequest, UserPairDeviceResponse
from app.services import pairing

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

    # The robot serves all of the owner's dogs (placeholder if the owner has none) and
    # learns them right away if online.
    pairing.device_dog_ids(device_id)
    await _push_profiles(device_id, user_id)

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


@router.delete("/devices/{device_id}")
async def delete_user_device(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Unpair a device from the current user by device_id in path.

    RESTful alternative to POST /api/user/unpair-device.
    Removes pairing from database and in-memory cache.
    """
    user_id = current_user["user_id"]

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
    logger.info(f"User {user_id} unpaired device {device_id} via DELETE - removed from DB")

    return {"status": "ok", "message": f"Device {device_id} unpaired"}


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


# ============== A robot's dogs = its owner's dogs (owner model 2026-11-07) ==============

async def _push_profiles(device_id: str, user_id: str) -> None:
    try:
        from app.routers.dogs import push_profiles_to_device
        await push_profiles_to_device(device_id, user_id)
    except Exception as e:  # offline robot gets profiles on its next connect
        logger.debug(f"[PAIRING] profiles push to {device_id} skipped: {e}")


def _owned_device(device_id: str, user_id: str) -> None:
    if db_get_device_owner(device_id) != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "device_not_paired", "device_id": device_id})


@router.get("/devices/{device_id}/dogs")
async def get_device_dogs(device_id: str, current_user: dict = Depends(get_current_user)):
    """The dogs this robot serves = all of the owner's dogs, oldest first (a placeholder is
    created if the owner has none). Robots and dogs are not linked individually."""
    _owned_device(device_id, current_user["user_id"])
    dogs = []
    for dog_id in pairing.device_dog_ids(device_id):
        d = get_dog_by_id(dog_id) or {}
        dogs.append({"dog_id": dog_id, "name": d.get("name"), "is_placeholder": bool(d.get("is_placeholder"))})
    return {"device_id": device_id, "dogs": dogs}


@router.put("/devices/{device_id}/dogs")
async def put_device_dogs(device_id: str, current_user: dict = Depends(get_current_user)):
    """Retired 2026-11-07: a robot serves all of its owner's dogs, so there is no per-robot
    dog list to edit. Add/remove dogs with the normal /api/dogs endpoints instead."""
    raise HTTPException(status_code=status.HTTP_410_GONE,
                        detail={"code": "robot_dog_pairing_removed",
                                "message": "Robots serve all of the owner's dogs; nothing to re-pair."})
