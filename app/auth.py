import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings, Settings

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# HTTP Bearer scheme for JWT tokens
security = HTTPBearer()


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    data: dict,
    settings: Settings = None,
    expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token."""
    if settings is None:
        settings = get_settings()

    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def decode_token(token: str, settings: Settings = None) -> Optional[dict]:
    """Decode and validate a JWT token."""
    if settings is None:
        settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings)
) -> dict:
    """Dependency to get the current authenticated user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(credentials.credentials, settings)
    if payload is None:
        raise credentials_exception

    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    return {"user_id": user_id, "email": payload.get("email")}


def generate_device_signature(device_id: str, device_secret: str) -> str:
    """Generate HMAC signature for device authentication."""
    message = device_id.encode()
    signature = hmac.new(
        device_secret.encode(),
        message,
        hashlib.sha256
    ).hexdigest()
    return signature


def verify_device_signature(device_id: str, signature: str, device_secret: str) -> bool:
    """Verify a device's HMAC signature."""
    expected = generate_device_signature(device_id, device_secret)
    return hmac.compare_digest(signature, expected)


def generate_pairing_code() -> str:
    """Generate a 6-character pairing code for device pairing."""
    return secrets.token_hex(3).upper()
