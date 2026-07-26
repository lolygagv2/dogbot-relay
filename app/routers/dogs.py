"""Dog profile management endpoints (Build 50 / Phase 1 A1)."""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
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
    merge_dogs,
    update_dog,
)
from app.models import DogPhoto, DogPhotoCreate, DogRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dogs", tags=["Dogs"])


# ============== camelCase request/response models ==============

class DogProfileWrite(BaseModel):
    """Accepts both camelCase (preferred) and snake_case keys."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # Client-supplied stable dog id (UUIDv7 or legacy dog_<epochms>). The app
    # sends this as `id` today; the spec name is `dog_id` — both are accepted.
    # Optional: older app builds omit it and the relay mints a fallback id.
    id: Optional[str] = Field(None, alias="dog_id")
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

def _dog_to_profile(dog: dict) -> dict[str, Any]:
    """Serialize a dog row to the robot-facing profile object (dog-id contract 2026-07-12).

    The robot keys its store on `dog_id` (the app's canonical, rename-stable id) and
    treats name/aruco_id/color as informational. `aruco_id` is the int marker the app
    configured (nullable); it is NOT the dog's identity.
    """
    return {
        "dog_id": dog["id"],
        "name": dog["name"],
        "aruco_id": dog.get("aruco_marker_id"),
        "color": dog.get("color"),
        "breed": dog.get("breed"),
        "photo_url": dog.get("profile_photo_url"),
        "photo_version": dog.get("photo_version") or 1,
    }


def build_user_profiles(user_id: str) -> list[dict[str, Any]]:
    """Build the {"type":"profiles"} payload list for all of a user's dogs."""
    return [_dog_to_profile(d) for d in get_user_dogs(user_id)]


async def push_profiles_to_device(device_id: str, user_id: str) -> bool:
    """Send the full profiles snapshot to a single robot (used on device connect)."""
    manager = get_connection_manager()
    profiles = build_user_profiles(user_id)
    sent = await manager.send_to_robot(device_id, {
        "type": "profiles",
        "profiles": profiles,
    })
    if sent:
        logger.info(f"[DOG-SYNC] Pushed {len(profiles)} profile(s) to device {device_id} for user {user_id}")
    else:
        logger.warning(f"[DOG-SYNC] profiles push not delivered to {device_id} (offline)")
    return sent


async def _notify_robots_reload_dogs(user_id: str):
    """Sync dogs to all of the user's robots.

    Pushes the full {"type":"profiles"} snapshot (canonical dog_id + aruco_id), then
    the legacy {"command":"reload_dogs"} nudge as a backward-compatible fallback for
    robots that haven't adopted the profiles push yet.
    """
    manager = get_connection_manager()
    devices = manager.get_user_devices(user_id)
    profiles = build_user_profiles(user_id)
    for device_id in devices:
        sent = await manager.send_to_robot(device_id, {
            "type": "profiles",
            "profiles": profiles,
        })
        await manager.send_to_robot(device_id, {
            "command": "reload_dogs",
        })
        if sent:
            logger.info(
                f"[DOG-SYNC] Synced {len(profiles)} profile(s) + reload_dogs to "
                f"device {device_id} for user {user_id}"
            )
        else:
            logger.warning(f"[DOG-SYNC] dog sync not delivered to {device_id} (offline)")


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
    response: Response,
) -> dict:
    """Create or update a dog profile — upsert by the client-supplied id.

    The relay NEVER mints an id when the client supplies one: honoring the app's
    id is what keeps a profile from forking on every logout/login. If the same id
    already exists for this owner, the POST updates that row (idempotent re-POST)
    and returns 200. A never-seen id (or an omitted id, for older app builds that
    get a minted fallback) creates a new row and returns 201.
    """
    user_id = current_user["user_id"]
    incoming_id = dog_data.id

    # ---- Upsert path: the client id already exists ----
    if incoming_id and get_dog_by_id(incoming_id) is not None:
        role = get_user_dog_role(user_id, incoming_id)
        if role is None:
            # The id exists but isn't this user's — never recycle an id across users.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Dog id already belongs to another user",
            )
        if role == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Viewers cannot modify dog profiles",
            )
        dog = update_dog(
            incoming_id,
            name=dog_data.name,
            breed=dog_data.breed,
            color=dog_data.color,
            profile_photo_url=dog_data.photo_url,
            weight=dog_data.weight,
            notes=dog_data.notes,
            aruco_marker_id=dog_data.aruco_marker_id,
            goals=dog_data.goals,
            last_mission_id=dog_data.last_mission_id,
            **({"photo_version": dog_data.photo_version} if dog_data.photo_version is not None else {}),
        )
        logger.info(f"User {user_id} upserted (updated) dog {incoming_id}: {dog_data.name}")
        await _notify_robots_reload_dogs(user_id)
        response.status_code = status.HTTP_200_OK
        return _to_response(dog)

    # ---- Create path: new id (or minted fallback) ----
    # Name-uniqueness only blocks a *different* dog reusing the name; the upsert
    # path above already handled a re-POST of an existing id.
    if check_duplicate_dog_name(user_id, dog_data.name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A dog named '{dog_data.name}' already exists"
        )

    dog_id = incoming_id or f"dog_{get_dog_count() + 1:06d}"

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

    logger.info(
        f"User {user_id} created dog {dog_id}: {dog_data.name}"
        f"{' (minted fallback id)' if not incoming_id else ''}"
    )

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


class DogMergeRequest(BaseModel):
    """Body for POST /api/dogs/merge — collapse dog-id variants server-side."""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    target_dog_id: str = Field(..., alias="targetDogId", description="Canonical dog that survives")
    source_dog_ids: list[str] = Field(..., alias="sourceDogIds", min_length=1,
                                      description="Variant dog ids to merge into the target")


@router.post("/merge", response_model=None)
async def merge_dog_profiles(
    body: DogMergeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Merge duplicate/variant dog profiles into one canonical dog_id.

    All history (activity events, mission log, metrics, schedules, photos,
    voice commands) is re-keyed onto the target; the source dog rows are
    deleted. Robots are pushed the updated profile snapshot afterwards so
    they re-key their local stores on the canonical id.
    """
    user_id = current_user["user_id"]
    try:
        result = merge_dogs(user_id, body.target_dog_id, body.source_dog_ids)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    logger.info(f"User {user_id} merged dogs {result['merged']} -> {result['target']}")

    await _notify_robots_reload_dogs(user_id)

    return result


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
