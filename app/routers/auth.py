from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import create_user, get_user_by_email, get_user_by_id, get_user_count
from app.models import TokenResponse, User, UserCreate, UserLogin

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


@router.post("/register", response_model=TokenResponse)
async def register(
    user_data: UserCreate,
    settings: Settings = Depends(get_settings)
):
    """Register a new user account."""
    email = user_data.email.lower()

    if get_user_by_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Create user
    user_id = f"user_{get_user_count() + 1:06d}"
    hashed_password = hash_password(user_data.password)
    create_user(user_id, email, hashed_password)

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

    user = get_user_by_email(email)
    if not user or not verify_password(credentials.password, user["hashed_password"]):
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
    user = get_user_by_email(email)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return User(
        user_id=user["user_id"],
        email=user["email"],
        devices=[],
        created_at=user["created_at"]
    )
