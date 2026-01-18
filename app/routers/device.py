from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, status

from app.auth import (
    generate_pairing_code,
    get_current_user,
    verify_device_signature,
)
from app.config import Settings, get_settings
from app.connection_manager import ConnectionManager, get_connection_manager
from app.models import (
    Device,
    DevicePair,
    DevicePairResponse,
    DeviceRegister,
    DeviceRegisterResponse,
)
from app.routers.auth import add_device_to_user

router = APIRouter(prefix="/api/device", tags=["Device Management"])

# In-memory device store (replace with database in production)
devices_db: dict[str, dict] = {}


@router.post("/register", response_model=DeviceRegisterResponse)
async def register_device(
    device_data: DeviceRegister,
    authorization: str = Header(...),
    settings: Settings = Depends(get_settings)
):
    """
    Register a robot device with the relay server.
    The Authorization header should contain HMAC-SHA256 signature.
    """
    # Extract signature from header (format: "HMAC-SHA256 {signature}")
    try:
        scheme, signature = authorization.split(" ", 1)
        if scheme != "HMAC-SHA256":
            raise ValueError("Invalid scheme")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )

    # Verify device signature
    if not verify_device_signature(device_data.device_id, signature, settings.device_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device signature"
        )

    # Generate pairing code for new devices
    pairing_code = generate_pairing_code()

    # Register or update device
    devices_db[device_data.device_id] = {
        "device_id": device_data.device_id,
        "name": f"WIM-Z {device_data.device_id[-6:]}",
        "owner_id": devices_db.get(device_data.device_id, {}).get("owner_id"),
        "is_online": False,
        "last_seen": datetime.now(timezone.utc),
        "firmware_version": device_data.firmware_version,
        "local_ip": None,
        "pairing_code": pairing_code
    }

    return DeviceRegisterResponse(
        success=True,
        websocket_url="wss://api.wimz.io/ws/device"
    )


@router.post("/pair", response_model=DevicePairResponse)
async def pair_device(
    pair_data: DevicePair,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Pair a device with the current user using a pairing code.
    """
    pairing_code = pair_data.pairing_code.upper()

    # Find device with this pairing code
    device_id = None
    for did, device in devices_db.items():
        if device.get("pairing_code") == pairing_code:
            device_id = did
            break

    if not device_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid pairing code"
        )

    device = devices_db[device_id]

    # Check if device is already owned by someone else
    if device.get("owner_id") and device["owner_id"] != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device is already paired with another user"
        )

    # Pair device with user
    user_id = current_user["user_id"]
    device["owner_id"] = user_id
    device["pairing_code"] = None  # Clear pairing code after successful pairing

    # Update connection manager
    manager.set_device_owner(device_id, user_id)

    # Add device to user's device list
    add_device_to_user(user_id, device_id)

    return DevicePairResponse(
        success=True,
        device_id=device_id
    )


@router.get("/list", response_model=list[Device])
async def list_devices(
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """List all devices owned by the current user."""
    user_id = current_user["user_id"]
    user_devices = []

    for device_id, device in devices_db.items():
        if device.get("owner_id") == user_id:
            user_devices.append(Device(
                device_id=device["device_id"],
                name=device["name"],
                owner_id=device["owner_id"],
                is_online=manager.is_robot_online(device_id),
                last_seen=device.get("last_seen"),
                firmware_version=device["firmware_version"],
                local_ip=device.get("local_ip")
            ))

    return user_devices


@router.get("/{device_id}", response_model=Device)
async def get_device(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """Get details of a specific device."""
    if device_id not in devices_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    device = devices_db[device_id]

    # Check ownership
    if device.get("owner_id") != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this device"
        )

    return Device(
        device_id=device["device_id"],
        name=device["name"],
        owner_id=device["owner_id"],
        is_online=manager.is_robot_online(device_id),
        last_seen=device.get("last_seen"),
        firmware_version=device["firmware_version"],
        local_ip=device.get("local_ip")
    )


@router.delete("/{device_id}")
async def unpair_device(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """Unpair a device from the current user."""
    if device_id not in devices_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    device = devices_db[device_id]

    # Check ownership
    if device.get("owner_id") != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to unpair this device"
        )

    # Unpair device
    device["owner_id"] = None
    device["pairing_code"] = generate_pairing_code()  # Generate new pairing code

    return {"success": True, "message": "Device unpaired successfully"}


def get_device_data(device_id: str) -> dict | None:
    """Helper to get device data."""
    return devices_db.get(device_id)


def update_device_online_status(device_id: str, is_online: bool):
    """Helper to update device online status."""
    if device_id in devices_db:
        devices_db[device_id]["is_online"] = is_online
        devices_db[device_id]["last_seen"] = datetime.now(timezone.utc)
