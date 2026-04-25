"""Dog profile management endpoints (Build 50 / Phase 1 A1)."""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import get_current_user
from app.connection_manager import get_connection_manager
from app.database import (
    add_user_dog,
    check_duplicate_dog_name,
    create_dog,
    create_dog_photo,
    delete_dog,
    get_dog_by_id,
    get_dog_count,
    get_dog_photos,
    get_photo_count,
    get_user_dog_role,
    get_user_dogs,
    update_dog,
)
from app.models import DogPhoto, DogPhotoCreate, DogRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dogs", tags=["Dogs"])


# ============== camelCase request/response models ==============

class DogProfileWrite(BaseModel):
    """Accepts both camelCase (preferred) and snake_case keys."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(..., min_length=1, max_length=50)
    breed: Optional[str] = None
    color: Optional[str] = None
    photo_url: Optional[str] = Field(None, alias="photoUrl")
    weight: Optional[float] = None
    notes: Optional[str] = None
    aruco_marker_id: Optional[int] = Field(None, alias="arucoMarkerId")
    goals: list[str] = Field(default_factory=list)
    last_mission_id: Optional[str] = Field(None, alias="lastMissionId")
    updated_at: str = Field(..., alias="updatedAt")
    photo_version: Optional[int] = Field(None, alias="photoVersion")


# ============== Helpers ==============

async def _notify_robots_reload_dogs(user_id: str):
    """Send reload_dogs command to all robots owned by this user."""
    manager = get_connection_manager()
    devices = manager.get_user_devices(user_id)
    for device_id in devices:
        sent = await manager.send_to_robot(device_id, {
            "command": "reload_dogs",
        })
        if sent:
            logger.info(f"[DOG-SYNC] Sent reload_dogs to device {device_id} for user {user_id}")
        else:
            logger.warning(f"[DOG-SYNC] reload_dogs not delivered to {device_id} (offline)")


def _to_response(dog: dict) -> dict[str, Any]:
    """Serialize a DB dog row to the camelCase wire format the app expects."""
    return {
        "id": dog["id"],
        "name": dog["name"],
        "breed": dog.get("breed"),
        "color": dog.get("color"),
        "photoUrl": dog.get("profile_photo_url"),
        "weight": dog.get("weight"),
        "notes": dog.get("notes"),
        "arucoMarkerId": dog.get("aruco_marker_id"),
        "goals": dog.get("goals") or [],
        "lastMissionId": dog.get("last_mission_id"),
        "createdAt": dog.get("created_at"),
        "updatedAt": dog.get("updated_at") or dog.get("created_at"),
        "photoVersion": dog.get("photo_version") or 1,
    }


# ============== Endpoints ==============

@router.get("", response_model=None)
async def list_user_dogs(
    current_user: Annotated[dict, Depends(get_current_user)]
) -> list[dict]:
    """List all dogs for the current user, ordered by createdAt ascending."""
    user_id = current_user["user_id"]
    logger.info(f"GET /api/dogs for user {user_id}")
    dogs = get_user_dogs(user_id)
    logger.info(f"Returning {len(dogs)} dog(s) for user {user_id}")
    return [_to_response(d) for d in dogs]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_dog_profile(
    dog_data: DogProfileWrite,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Create a new dog profile. The current user becomes the owner.

    Body must include updatedAt; server-generated updated_at takes precedence
    on conflict, so the field is recorded but the server clock authoritative.
    """
    user_id = current_user["user_id"]

    if check_duplicate_dog_name(user_id, dog_data.name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A dog named '{dog_data.name}' already exists"
        )

    count = get_dog_count()
    dog_id = f"dog_{count + 1:06d}"

    dog = create_dog(
        dog_id=dog_id,
        name=dog_data.name,
        user_id=user_id,
        breed=dog_data.breed,
        color=dog_data.color,
        profile_photo_url=dog_data.photo_url,
        aruco_marker_id=dog_data.aruco_marker_id,
        weight=dog_data.weight,
        notes=dog_data.notes,
        goals=dog_data.goals,
        last_mission_id=dog_data.last_mission_id,
        photo_version=dog_data.photo_version or 1,
    )

    add_user_dog(user_id, dog_id, "owner")

    logger.info(f"User {user_id} created dog {dog_id}: {dog_data.name}")

    await _notify_robots_reload_dogs(user_id)

    return _to_response(dog)


@router.get("/{dog_id}", response_model=None)
async def get_dog_profile(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Get a dog's profile. User must have access to the dog."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    dog = get_dog_by_id(dog_id)
    if dog is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    return _to_response(dog)


@router.put("/{dog_id}", response_model=None)
async def update_dog_profile(
    dog_id: str,
    dog_data: DogProfileWrite,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Update a dog's profile. Requires owner or caretaker role."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    if role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot update dog profiles"
        )

    fields: dict[str, Any] = {
        "name": dog_data.name,
        "breed": dog_data.breed,
        "color": dog_data.color,
        "profile_photo_url": dog_data.photo_url,
        "weight": dog_data.weight,
        "notes": dog_data.notes,
        "aruco_marker_id": dog_data.aruco_marker_id,
        "goals": dog_data.goals,
        "last_mission_id": dog_data.last_mission_id,
    }
    if dog_data.photo_version is not None:
        fields["photo_version"] = dog_data.photo_version

    dog = update_dog(dog_id, **fields)
    if dog is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    logger.info(f"User {user_id} updated dog {dog_id}")

    await _notify_robots_reload_dogs(user_id)

    return _to_response(dog)


@router.delete("/{dog_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dog_profile(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Delete a dog profile. Requires owner role."""
    user_id = current_user["user_id"]
    logger.info(f"DELETE /api/dogs/{dog_id} for user {user_id}")

    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        logger.warning(f"Dog {dog_id} not found or not accessible by user {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    if role != "owner":
        logger.warning(f"User {user_id} has role '{role}' for dog {dog_id}, owner required")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can delete dog profiles"
        )

    deleted = delete_dog(dog_id, user_id=user_id)
    if not deleted:
        logger.warning(f"Dog {dog_id} delete failed for user {user_id} (ownership mismatch)")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    logger.info(f"Deleted dog {dog_id} for user {user_id}")

    await _notify_robots_reload_dogs(user_id)


@router.get("/{dog_id}/photos", response_model=list[DogPhoto])
async def list_dog_photos(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Get all photos for a dog. User must have access to the dog."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    photos = get_dog_photos(dog_id)

    return [
        DogPhoto(
            id=photo["id"],
            dog_id=photo["dog_id"],
            photo_url=photo["photo_url"],
            is_profile_photo=photo["is_profile_photo"],
            captured_at=datetime.fromisoformat(photo["captured_at"].replace("Z", "+00:00"))
        )
        for photo in photos
    ]


@router.post("/{dog_id}/photos", response_model=DogPhoto, status_code=status.HTTP_201_CREATED)
async def add_dog_photo(
    dog_id: str,
    photo_data: DogPhotoCreate,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Add a photo to a dog. Requires owner or caretaker role."""
    user_id = current_user["user_id"]

    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    if role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot add photos"
        )

    dog = get_dog_by_id(dog_id)
    if dog is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    count = get_photo_count()
    photo_id = f"photo_{count + 1:06d}"

    photo = create_dog_photo(
        photo_id=photo_id,
        dog_id=dog_id,
        photo_url=photo_data.photo_url,
        is_profile_photo=photo_data.is_profile_photo
    )

    # Bump photo_version on the dog so app caches refresh
    if photo_data.is_profile_photo:
        current_version = dog.get("photo_version") or 1
        update_dog(dog_id, photo_version=current_version + 1, profile_photo_url=photo_data.photo_url)

    logger.info(f"User {user_id} added photo {photo_id} to dog {dog_id}")

    return DogPhoto(
        id=photo["id"],
        dog_id=photo["dog_id"],
        photo_url=photo["photo_url"],
        is_profile_photo=photo["is_profile_photo"],
        captured_at=datetime.fromisoformat(photo["captured_at"].replace("Z", "+00:00"))
    )
