"""Dog profile management endpoints."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user
from app.database import (
    add_user_dog,
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
from app.models import Dog, DogCreate, DogPhoto, DogPhotoCreate, DogRole, DogUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dogs", tags=["Dogs"])


def _parse_datetime(dt_str: str) -> datetime:
    """Parse ISO datetime string to datetime object."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


@router.get("", response_model=list[Dog])
async def list_user_dogs(
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """List all dogs for the current user with their role."""
    user_id = current_user["user_id"]
    dogs = get_user_dogs(user_id)

    return [
        Dog(
            id=dog["id"],
            name=dog["name"],
            breed=dog["breed"],
            color=dog["color"],
            profile_photo_url=dog["profile_photo_url"],
            aruco_marker_id=dog["aruco_marker_id"],
            role=DogRole(dog["role"]),
            created_at=_parse_datetime(dog["created_at"])
        )
        for dog in dogs
    ]


@router.post("", response_model=Dog, status_code=status.HTTP_201_CREATED)
async def create_dog_profile(
    dog_data: DogCreate,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Create a new dog profile. The current user becomes the owner."""
    user_id = current_user["user_id"]

    # Generate dog ID
    count = get_dog_count()
    dog_id = f"dog_{count + 1:06d}"

    # Create the dog
    dog = create_dog(
        dog_id=dog_id,
        name=dog_data.name,
        breed=dog_data.breed,
        color=dog_data.color.value if dog_data.color else None,
        aruco_marker_id=dog_data.aruco_marker_id
    )

    # Add user as owner
    add_user_dog(user_id, dog_id, "owner")

    logger.info(f"User {user_id} created dog {dog_id}: {dog_data.name}")

    return Dog(
        id=dog["id"],
        name=dog["name"],
        breed=dog["breed"],
        color=dog["color"],
        profile_photo_url=dog["profile_photo_url"],
        aruco_marker_id=dog["aruco_marker_id"],
        role=DogRole.OWNER,
        created_at=_parse_datetime(dog["created_at"])
    )


@router.get("/{dog_id}", response_model=Dog)
async def get_dog_profile(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Get a dog's profile. User must have access to the dog."""
    user_id = current_user["user_id"]

    # Check user has access
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

    return Dog(
        id=dog["id"],
        name=dog["name"],
        breed=dog["breed"],
        color=dog["color"],
        profile_photo_url=dog["profile_photo_url"],
        aruco_marker_id=dog["aruco_marker_id"],
        role=DogRole(role),
        created_at=_parse_datetime(dog["created_at"])
    )


@router.put("/{dog_id}", response_model=Dog)
async def update_dog_profile(
    dog_id: str,
    dog_data: DogUpdate,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Update a dog's profile. Requires owner or caretaker role."""
    user_id = current_user["user_id"]

    # Check user has access and appropriate role
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

    # Build update fields
    update_fields = {}
    if dog_data.name is not None:
        update_fields["name"] = dog_data.name
    if dog_data.breed is not None:
        update_fields["breed"] = dog_data.breed
    if dog_data.color is not None:
        update_fields["color"] = dog_data.color.value
    if dog_data.profile_photo_url is not None:
        update_fields["profile_photo_url"] = dog_data.profile_photo_url
    if dog_data.aruco_marker_id is not None:
        update_fields["aruco_marker_id"] = dog_data.aruco_marker_id

    dog = update_dog(dog_id, **update_fields)
    if dog is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    logger.info(f"User {user_id} updated dog {dog_id}")

    return Dog(
        id=dog["id"],
        name=dog["name"],
        breed=dog["breed"],
        color=dog["color"],
        profile_photo_url=dog["profile_photo_url"],
        aruco_marker_id=dog["aruco_marker_id"],
        role=DogRole(role),
        created_at=_parse_datetime(dog["created_at"])
    )


@router.delete("/{dog_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dog_profile(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Delete a dog profile. Requires owner role."""
    user_id = current_user["user_id"]

    # Check user is owner
    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found or access denied"
        )

    if role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners can delete dog profiles"
        )

    deleted = delete_dog(dog_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    logger.info(f"User {user_id} deleted dog {dog_id}")


@router.get("/{dog_id}/photos", response_model=list[DogPhoto])
async def list_dog_photos(
    dog_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Get all photos for a dog. User must have access to the dog."""
    user_id = current_user["user_id"]

    # Check user has access
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
            captured_at=_parse_datetime(photo["captured_at"])
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

    # Check user has access and appropriate role
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

    # Verify dog exists
    dog = get_dog_by_id(dog_id)
    if dog is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dog not found"
        )

    # Generate photo ID
    count = get_photo_count()
    photo_id = f"photo_{count + 1:06d}"

    photo = create_dog_photo(
        photo_id=photo_id,
        dog_id=dog_id,
        photo_url=photo_data.photo_url,
        is_profile_photo=photo_data.is_profile_photo
    )

    logger.info(f"User {user_id} added photo {photo_id} to dog {dog_id}")

    return DogPhoto(
        id=photo["id"],
        dog_id=photo["dog_id"],
        photo_url=photo["photo_url"],
        is_profile_photo=photo["is_profile_photo"],
        captured_at=_parse_datetime(photo["captured_at"])
    )
