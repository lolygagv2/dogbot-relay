from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import Settings, get_settings
from app.models import TokenResponse, User, UserCreate, UserLogin

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

# In-memory user store (replace with database in production)
users_db: dict[str, dict] = {}


@router.post("/register", response_model=TokenResponse)
async def register(
    user_data: UserCreate,
    settings: Settings = Depends(get_settings)
):
    """Register a new user account."""
    email = user_data.email.lower()

    if email in users_db:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Create user
    user_id = f"user_{len(users_db) + 1:06d}"
    users_db[email] = {
        "user_id": user_id,
        "email": email,
        "password_hash": hash_password(user_data.password),
        "devices": [],
        "created_at": datetime.now(timezone.utc)
    }

    # Generate token
    token = create_access_token(
        data={"sub": user_id, "email": email},
        settings=settings
    )

    return TokenResponse(
        token=token,
        expires_in=settings.jwt_expire_minutes * 60
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: UserLogin,
    settings: Settings = Depends(get_settings)
):
    """Authenticate user and return JWT token."""
    email = credentials.email.lower()

    user = users_db.get(email)
    if not user or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    token = create_access_token(
        data={"sub": user["user_id"], "email": email},
        settings=settings
    )

    return TokenResponse(
        token=token,
        expires_in=settings.jwt_expire_minutes * 60
    )


@router.get("/me", response_model=User)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user information."""
    email = current_user["email"]
    user = users_db.get(email)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return User(
        user_id=user["user_id"],
        email=user["email"],
        devices=user["devices"],
        created_at=user["created_at"]
    )


def get_user_by_id(user_id: str) -> dict | None:
    """Helper to lookup user by ID."""
    for user in users_db.values():
        if user["user_id"] == user_id:
            return user
    return None


def add_device_to_user(user_id: str, device_id: str):
    """Helper to add a device to a user's device list."""
    for user in users_db.values():
        if user["user_id"] == user_id:
            if device_id not in user["devices"]:
                user["devices"].append(device_id)
            return True
    return False
