import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import (
    create_reset_code,
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_next_user_number,
    get_valid_reset_code,
    invalidate_reset_codes,
    update_user_password,
)
from app.models import (
    PasswordResetConfirm,
    PasswordResetRequest,
    TokenResponse,
    User,
    UserCreate,
    UserLogin,
)
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

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
    user_id = f"user_{get_next_user_number():06d}"
    hashed_password = hash_password(user_data.password)
    create_user(user_id, email, hashed_password)

    # Generate token
    token = create_access_token(
        data={"sub": user_id, "email": email},
        settings=settings
    )

    return TokenResponse(
        token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user_id=user_id,
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
        expires_in=settings.jwt_expire_minutes * 60,
        user_id=user["user_id"],
    )


@router.get("/validate")
async def validate_token(current_user: dict = Depends(get_current_user)):
    """Validate a JWT token. Returns user info if valid, 401 if not."""
    user = get_user_by_email(current_user["email"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return {
        "valid": True,
        "user_id": user["user_id"],
        "email": user["email"],
    }


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


@router.post("/request-reset")
async def request_password_reset(body: PasswordResetRequest):
    """Request a password reset code. Always returns 200 to avoid leaking whether email exists."""
    email = body.email.lower()

    user = get_user_by_email(email)
    if user:
        code = email_service.generate_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        create_reset_code(email, code, expires_at)
        email_service.send_reset_code(email, code)
        logger.info(f"[AUTH] Password reset requested for {email}")
    else:
        logger.info(f"[AUTH] Password reset requested for unknown email {email}")

    return {"message": "If that email is registered, a reset code has been sent."}


@router.post("/reset-password", response_model=TokenResponse)
async def reset_password(
    body: PasswordResetConfirm,
    settings: Settings = Depends(get_settings),
):
    """Verify reset code and update password. Returns a new JWT token on success."""
    email = body.email.lower()

    reset_code = get_valid_reset_code(email, body.code)
    if not reset_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset code",
        )

    # Update password
    hashed = hash_password(body.new_password)
    updated = update_user_password(email, hashed)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Invalidate all codes for this email
    invalidate_reset_codes(email)

    # Return a fresh token so user is logged in
    user = get_user_by_email(email)
    token = create_access_token(
        data={"sub": user["user_id"], "email": email},
        settings=settings,
    )

    logger.info(f"[AUTH] Password reset completed for {email}")

    return TokenResponse(
        token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user_id=user["user_id"],
    )
