"""Robot OTA release distribution (updater contract 2026-08-31, robot c28ed1f).

Three consumers, three auth schemes:

  * Robot updater — GET /api/releases/{version} and .../download, authenticated
    with per-device HMAC headers (X-Device-Id / X-Timestamp / X-Signature over
    "{device_id}:{timestamp}"). Signed fresh per request, verified strictly with
    a ±5 min freshness window.
  * Mobile app — GET /api/releases/latest, authenticated with the ordinary user
    JWT, to compare the served version against the robot's telemetry sw_version.
  * Morgan — POST /api/releases to upload an artifact, admin JWT only.

Version strings are opaque. The app compares them with plain inequality (no
semver ordering), so a version must never be reused for different bytes.
"""
import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from app.auth import get_current_user, verify_device_request_signature
from app.config import Settings, get_settings
from app.database import (
    get_device_secret,
    get_latest_release,
    get_release,
    get_release_filename,
    get_user_by_id,
    insert_release,
    list_releases,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/releases", tags=["Releases"])

# Max artifact size: 500MB. A repo-tree tarball is far smaller, but the ceiling
# exists so a malformed upload can't fill the Lightsail disk.
MAX_ARTIFACT_SIZE = 500 * 1024 * 1024

# Streaming read size for hashing and disk writes.
_CHUNK_SIZE = 1024 * 1024

# Version strings become path components, so constrain them to characters that
# cannot escape the storage root or confuse the filesystem.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _resolve_storage_root() -> Path:
    configured = get_settings().releases_storage_dir
    if configured:
        return Path(configured)
    return Path(__file__).parent.parent.parent / "data" / "releases"


# Storage root: <root>/<version>.tar.gz — must be persistent; the manifest rows
# outlive /tmp across reboots, which would leave the robot downloading a 404.
STORAGE_ROOT = _resolve_storage_root()
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


def _validate_version(version: str) -> str:
    """Reject version strings that are unsafe as path components."""
    if not _VERSION_RE.match(version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid version string",
        )
    return version


def require_device(
    x_device_id: Annotated[Optional[str], Header()] = None,
    x_timestamp: Annotated[Optional[str], Header()] = None,
    x_signature: Annotated[Optional[str], Header()] = None,
    settings: Settings = Depends(get_settings),
) -> str:
    """Authenticate a robot via signed headers. Returns the device_id.

    Mirrors the device WS handshake's secret lookup (per-device secret, falling
    back to the shared settings secret) but verifies strictly — see
    verify_device_request_signature for why the permissive WS verifier is not
    reused here.
    """
    if not x_device_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Device-Id",
        )

    device_secret = get_device_secret(x_device_id) or settings.device_secret
    ok, reason = verify_device_request_signature(
        x_device_id, x_timestamp, x_signature, device_secret
    )
    if not ok:
        logger.warning(f"[RELEASE] Device auth rejected for {x_device_id}: {reason}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Device authentication failed ({reason})",
        )

    return x_device_id


def require_admin(
    current_user: Annotated[dict, Depends(get_current_user)],
    settings: Settings = Depends(get_settings),
) -> dict:
    """Allow only accounts listed in settings.admin_emails."""
    admins = {e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()}
    email = (current_user.get("email") or "").lower()

    # A token minted before the email claim existed carries only user_id; fall
    # back to the account row rather than refusing a genuine admin.
    if not email:
        user = get_user_by_id(current_user["user_id"])
        email = (user["email"] or "").lower() if user else ""

    if email not in admins:
        logger.warning(f"[RELEASE] Admin-only endpoint refused for {email or current_user.get('user_id')}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    return current_user


@router.get("/latest")
async def latest_release(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Return the newest release manifest for the app's availability compare.

    404 when no release has been uploaded — the app treats that as "no releases
    available" and silently shows "Up to date".
    """
    manifest = get_latest_release()
    if not manifest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No releases available",
        )

    return manifest


@router.get("")
async def list_all_releases(
    admin: Annotated[dict, Depends(require_admin)],
):
    """List every uploaded release, newest first. Admin only."""
    return {"releases": list_releases()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_release(
    admin: Annotated[dict, Depends(require_admin)],
    version: str = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    min_updater_version: Optional[str] = Form(None),
):
    """Upload a release artifact (.tar.gz of the repo tree). Admin only.

    The artifact streams to a temp file while being hashed, so a 500MB upload
    never lands in memory and a failed/oversized upload leaves no partial file
    in the storage root.
    """
    _validate_version(version)

    if get_release(version) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Release {version} already exists; versions are immutable",
        )

    digest = hashlib.sha256()
    size = 0
    tmp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=STORAGE_ROOT, prefix=".upload-", suffix=".part", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := await file.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_ARTIFACT_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Artifact exceeds {MAX_ARTIFACT_SIZE // (1024 * 1024)}MB limit",
                    )
                digest.update(chunk)
                tmp.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Empty artifact",
            )

        filename = f"{version}.tar.gz"
        final_path = STORAGE_ROOT / filename
        shutil.move(str(tmp_path), str(final_path))
        tmp_path = None

        manifest = insert_release(
            version=version,
            sha256=digest.hexdigest(),
            size_bytes=size,
            filename=filename,
            notes=notes,
            min_updater_version=min_updater_version,
        )
    except ValueError as e:
        # Lost a race against a concurrent upload of the same version.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    logger.info(
        f"[RELEASE] {admin.get('email')} uploaded {version} "
        f"({size} bytes, sha256={manifest['sha256'][:12]}…)"
    )
    return manifest


@router.get("/{version}")
async def release_manifest(
    version: str,
    device_id: Annotated[str, Depends(require_device)],
):
    """Return the manifest for a specific version. 404 for unknown versions."""
    _validate_version(version)

    manifest = get_release(version)
    if not manifest:
        logger.info(f"[RELEASE] Device {device_id} requested unknown version {version}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown release {version}",
        )

    return manifest


@router.get("/{version}/download")
async def download_release(
    version: str,
    device_id: Annotated[str, Depends(require_device)],
):
    """Stream the release artifact to the robot updater.

    FileResponse sets Content-Length from the file size, which is what drives the
    robot's progress_pct.
    """
    _validate_version(version)

    filename = get_release_filename(version)
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown release {version}",
        )

    path = STORAGE_ROOT / filename
    if not path.is_file():
        # Manifest row without bytes — the storage root was wiped or the upload
        # was rolled back. 404 rather than 500: the updater's retry path treats
        # it as "not available yet".
        logger.error(f"[RELEASE] Manifest exists but artifact missing: {path}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact for {version} is not available",
        )

    logger.info(f"[RELEASE] Device {device_id} downloading {version} ({path.stat().st_size} bytes)")
    return FileResponse(
        path=str(path),
        media_type="application/gzip",
        filename=filename,
    )
