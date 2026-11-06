import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.config import get_settings, Settings

# HTTP Bearer scheme for JWT tokens
security = HTTPBearer()


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    truncated = password.encode('utf-8')[:72]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    truncated = plain_password.encode('utf-8')[:72]
    return bcrypt.checkpw(truncated, hashed_password.encode('utf-8'))


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


def verify_device_signature_with_timestamp(
    device_id: str,
    timestamp: str | None,
    signature: str,
    device_secret: str
) -> bool:
    """
    Verify device signature, trying multiple formats for flexibility.

    Tries these message formats:
    1. device_id + timestamp (concatenated)
    2. device_id:timestamp (colon-separated)
    3. timestamp + device_id
    4. device_id only (no timestamp)

    Returns True if any format matches.
    """
    import logging
    logger = logging.getLogger(__name__)

    def compute_sig(message: str) -> str:
        return hmac.new(
            device_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

    # Build list of messages to try
    messages_to_try = []

    if timestamp:
        messages_to_try.extend([
            f"{device_id}{timestamp}",      # device_id + timestamp
            f"{device_id}:{timestamp}",     # device_id:timestamp
            f"{timestamp}{device_id}",      # timestamp + device_id
            f"{timestamp}:{device_id}",     # timestamp:device_id
        ])

    # Always try device_id only as fallback
    messages_to_try.append(device_id)

    # Try each format
    for msg in messages_to_try:
        expected = compute_sig(msg)
        if hmac.compare_digest(signature.lower(), expected.lower()):
            logger.info(f"Signature matched with message format: {msg[:20]}...")
            return True
        logger.debug(f"Tried '{msg[:30]}...': expected={expected[:16]}, got={signature[:16]}")

    # Log all attempted formats for debugging
    logger.warning(f"No signature format matched for device {device_id}")
    logger.warning(f"  Received signature: {signature}")
    logger.warning(f"  Timestamp provided: {timestamp}")
    for msg in messages_to_try[:3]:  # Log first 3 attempts
        logger.warning(f"  Tried '{msg}' -> {compute_sig(msg)}")

    return False


def generate_pairing_code() -> str:
    """Generate a 6-character pairing code for device pairing."""
    return secrets.token_hex(3).upper()


# Maximum clock skew accepted on a signed device HTTP request, in seconds.
# The robot signs fresh per request (updater contract 2026-08-31), so this only
# needs to cover clock drift and flight time, not a session lifetime.
DEVICE_REQUEST_MAX_SKEW_SECONDS = 300


def verify_device_request_signature(
    device_id: str,
    timestamp: str | None,
    signature: str | None,
    device_secret: str,
) -> tuple[bool, str]:
    """Strictly verify a signed device HTTP request.

    Unlike verify_device_signature_with_timestamp (which tries five message
    formats and falls back to signing device_id ALONE), this accepts exactly one
    format — HMAC-SHA256("{device_id}:{timestamp}") — and requires a fresh
    timestamp. The permissive verifier's device_id-only fallback makes a captured
    signature valid forever, which is acceptable for the WS handshake it was
    written for but not for release artifact downloads.

    Returns (ok, reason). ``reason`` is a short code for logging on failure.
    """
    import logging
    logger = logging.getLogger(__name__)

    if not signature or not timestamp:
        return False, "missing_headers"

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "bad_timestamp"

    skew = abs(time.time() - ts)
    if skew > DEVICE_REQUEST_MAX_SKEW_SECONDS:
        logger.warning(
            f"[RELEASE-AUTH] Stale timestamp for {device_id}: skew={skew:.0f}s "
            f"(max {DEVICE_REQUEST_MAX_SKEW_SECONDS}s)"
        )
        return False, "stale_timestamp"

    expected = hmac.new(
        device_secret.encode(),
        f"{device_id}:{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature.strip().lower(), expected):
        logger.warning(f"[RELEASE-AUTH] Signature mismatch for {device_id}")
        return False, "bad_signature"

    return True, "ok"
# ============================================================================
# Dec 9 demo build (2026-11-06): one actor resolver for app / admin / internal
# callers (MCP connector, voice agent) / robot. Used by the event store,
# settings, sounds and robot-control routes so guards live in one place.
# ============================================================================

from dataclasses import dataclass, field
from fastapi import Header, Request


@dataclass
class Actor:
    kind: str                      # app | admin | connector | agent | robot
    id: str                        # user_id, client_id, "agent", or device_id
    user_id: Optional[str] = None  # the WIM-Z user this call acts for
    email: Optional[str] = None
    device_id: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Audit string, e.g. 'app:user_000003', 'connector:abc123', 'agent', 'robot:wimz_robot_01'."""
        return self.kind if self.kind == "agent" else f"{self.kind}:{self.id}"

    @property
    def is_internal(self) -> bool:
        return self.kind in ("connector", "agent")


def _is_admin_email(email: Optional[str], settings: Settings) -> bool:
    if not email:
        return False
    admins = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    return email.lower() in admins


async def resolve_actor(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
    x_on_behalf_of: Optional[str] = Header(default=None, alias="X-On-Behalf-Of"),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-ID"),
    x_timestamp: Optional[str] = Header(default=None, alias="X-Timestamp"),
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
    settings: Settings = Depends(get_settings),
) -> Actor:
    """Resolve who is calling.

    - `Authorization: Bearer <jwt>`                      -> app (admin if email is in admin_emails)
    - `Authorization: Bearer <internal_api_token>`
      + `X-Actor: connector:<client_id>|agent`
      + `X-On-Behalf-Of: <user_id>`                      -> connector / agent
    - `Authorization: HMAC-SHA256 <sig>` + `X-Device-ID`
      (+ `X-Timestamp`), or `X-Signature` header         -> robot
    """
    from app.database import get_device_owner, get_device_secret, get_user_by_id

    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                 detail={"code": "unauthorized"},
                                 headers={"WWW-Authenticate": "Bearer"})

    scheme, _, credential = (authorization or "").partition(" ")
    scheme = scheme.lower()
    credential = credential.strip()

    # --- robot HMAC ---
    if scheme in ("hmac-sha256", "hmac") or (x_signature and x_device_id):
        signature = credential if scheme.startswith("hmac") else (x_signature or "")
        device_id = x_device_id or request.query_params.get("device_id")
        if not device_id or not signature:
            raise unauthorized
        secret = get_device_secret(device_id) or settings.device_secret
        if not verify_device_signature_with_timestamp(device_id, x_timestamp, signature, secret):
            raise unauthorized
        owner = get_device_owner(device_id)
        if not owner:
            raise HTTPException(status_code=403, detail={"code": "device_not_paired"})
        return Actor(kind="robot", id=device_id, user_id=owner, device_id=device_id)

    if scheme != "bearer" or not credential:
        raise unauthorized

    # --- internal callers (same box) ---
    if x_actor:
        if not settings.internal_api_token:
            raise HTTPException(status_code=503, detail={"code": "internal_api_disabled"})
        if not secrets.compare_digest(credential, settings.internal_api_token):
            raise unauthorized
        if x_actor == "agent":
            kind, actor_id = "agent", "agent"
        elif x_actor.startswith("connector:") and len(x_actor) > len("connector:"):
            kind, actor_id = "connector", x_actor.split(":", 1)[1][:128]
        else:
            raise HTTPException(status_code=401, detail={"code": "bad_actor_header"})
        if not x_on_behalf_of:
            raise HTTPException(status_code=401, detail={"code": "missing_on_behalf_of"})
        if not get_user_by_id(x_on_behalf_of):
            raise HTTPException(status_code=401, detail={"code": "unknown_user"})
        return Actor(kind=kind, id=actor_id, user_id=x_on_behalf_of)

    # --- app JWT ---
    payload = decode_token(credential, settings)
    if not payload or not payload.get("sub"):
        raise unauthorized
    email = payload.get("email")
    kind = "admin" if _is_admin_email(email, settings) else "app"
    return Actor(kind=kind, id=payload["sub"], user_id=payload["sub"], email=email,
                 extra={"imp": payload.get("imp")})


async def require_admin(actor: Actor = Depends(resolve_actor)) -> Actor:
    if actor.kind != "admin":
        raise HTTPException(status_code=403, detail={"code": "admin_only"})
    return actor


def check_dog_access(actor: Actor, dog_id: str, write: bool = False) -> str:
    """Raise 403/404 unless the actor may touch this dog. Returns the dog's owner user_id."""
    from app.database import get_dog_by_id, get_dog_owner, get_user_dog_role

    dog = get_dog_by_id(dog_id)
    if not dog:
        raise HTTPException(status_code=404, detail={"code": "dog_not_found"})
    owner = get_dog_owner(dog_id)
    if actor.kind == "admin":
        return owner or actor.user_id
    if actor.kind == "robot":
        if owner and owner == actor.user_id:
            return owner
        raise HTTPException(status_code=403, detail={"code": "dog_not_owned_by_device_owner"})
    role = get_user_dog_role(actor.user_id, dog_id) if actor.user_id else None
    if not role:
        raise HTTPException(status_code=403, detail={"code": "dog_access_denied"})
    if write and role == "viewer":
        raise HTTPException(status_code=403, detail={"code": "viewer_read_only"})
    return owner or actor.user_id
