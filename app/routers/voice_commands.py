"""Voice command sync endpoints (Phase 2 / A2).

App uploads per-dog WAV clips (e.g. recall, sit). Relay stores them on local
disk under voice_commands/<user_id>/<dog_id>/<command_id>.wav, persists a
metadata row, exposes a relay-served download URL, and notifies the user's
robot via WebSocket so it can prefetch the new audio.
"""
import logging
import os
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from app.auth import get_current_user
from app.config import get_settings
from app.connection_manager import get_connection_manager
from app.database import (
    delete_voice_command,
    get_user_dog_role,
    get_voice_command,
    list_voice_commands,
    list_voice_commands_for_user,
    upsert_voice_command,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice-commands", tags=["VoiceCommands"])


def _resolve_storage_root() -> Path:
    configured = get_settings().voice_storage_dir
    if configured:
        return Path(configured)
    return Path(__file__).parent.parent.parent / "data" / "voice_commands"


# Storage root: <root>/<user_id>/<dog_id>/<command_id>.wav — must be persistent;
# DB rows outlive /tmp across reboots, leaving 404s on download (contract 2026-07-30).
STORAGE_ROOT = _resolve_storage_root()
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

# Pre-2026-07-30 storage root. Files still there (i.e. no reboot since upload)
# are moved into STORAGE_ROOT at import.
LEGACY_STORAGE_ROOT = Path("/tmp/wimz-voice-commands")


def _migrate_legacy_files() -> None:
    if LEGACY_STORAGE_ROOT == STORAGE_ROOT or not LEGACY_STORAGE_ROOT.is_dir():
        return
    moved = 0
    try:
        for wav in LEGACY_STORAGE_ROOT.rglob("*.wav"):
            dest = STORAGE_ROOT / wav.relative_to(LEGACY_STORAGE_ROOT)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(wav), str(dest))
            moved += 1
    except Exception as e:
        logger.warning(f"[VOICE-CMD] legacy /tmp migration failed: {e}")
    if moved:
        logger.info(f"[VOICE-CMD] Migrated {moved} file(s) from {LEGACY_STORAGE_ROOT} to {STORAGE_ROOT}")


_migrate_legacy_files()

# Conservative cap — voice clips are short
MAX_FILE_SIZE = 5 * 1024 * 1024


def _sanitize_id_segment(value: str) -> str:
    """Reject any path traversal characters; allow alnum, dash, underscore, dot."""
    cleaned = "".join(c for c in value if c.isalnum() or c in "-_.")
    if not cleaned or cleaned.startswith(".") or cleaned != value:
        raise HTTPException(status_code=400, detail=f"Invalid id segment: {value!r}")
    return cleaned


def _file_path(user_id: str, dog_id: str, command_id: str) -> Path:
    return STORAGE_ROOT / user_id / dog_id / f"{command_id}.wav"


def _audio_url(user_id: str, dog_id: str, command_id: str) -> str:
    return f"/api/voice-commands/file/{user_id}/{dog_id}/{command_id}"


def _to_response(row: dict) -> dict:
    return {
        "command_id": row["command_id"],
        # Stamp the owning dog on every entry so the app can drop any manifest
        # row whose dog_id doesn't match the dog it requested (defense-in-depth
        # against id confusion — see relay contract fix 2026-07-12).
        "dog_id": row["dog_id"],
        "audio_url": _audio_url(row["user_id"], row["dog_id"], row["command_id"]),
        "updated_at": row["updated_at"],
        "format": row["format"],
        "size_bytes": row["size_bytes"],
    }


async def _push_to_robots(user_id: str, message: dict) -> None:
    """Forward a voice_command_* event to all of the user's connected robots."""
    manager = get_connection_manager()
    for device_id in manager.get_user_devices(user_id):
        delivered = await manager.send_to_robot(device_id, message)
        if delivered:
            logger.info(
                f"[VOICE-CMD] Notified robot {device_id}: {message.get('type')} "
                f"dog={message.get('dog_id')} command={message.get('command_id')}"
            )
        else:
            logger.warning(f"[VOICE-CMD] Robot {device_id} offline; skipping {message.get('type')}")


async def replay_voice_commands_to_device(device_id: str, user_id: str) -> None:
    """Re-push voice_command_updated for every stored row on robot connect.

    Uploads made while the robot was offline are otherwise lost —
    _push_to_robots skips disconnected robots and nothing catches them up
    (contract 2026-07-30). Idempotent on the robot side: it overwrites by
    (dog_id, command_id) or skips on matching updated_at.
    """
    rows = list_voice_commands_for_user(user_id)
    if not rows:
        return
    manager = get_connection_manager()
    sent = 0
    for row in rows:
        delivered = await manager.send_to_robot(device_id, {
            "type": "voice_command_updated",
            "dog_id": row["dog_id"],
            "command_id": row["command_id"],
            "audio_url": _audio_url(user_id, row["dog_id"], row["command_id"]),
            "updated_at": row["updated_at"],
        })
        if not delivered:
            break
        sent += 1
    logger.info(
        f"[VOICE-CMD] Replayed {sent}/{len(rows)} voice commands to robot "
        f"{device_id} (user={user_id})"
    )


def _require_dog_access(user_id: str, dog_id: str) -> None:
    role = get_user_dog_role(user_id, dog_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Dog not found or access denied")


# ============== Endpoints ==============

@router.get("")
async def list_commands(
    current_user: Annotated[dict, Depends(get_current_user)],
    dog_id: str = Query(..., description="Dog ID to scope to"),
):
    """List voice commands for a given dog."""
    user_id = current_user["user_id"]
    dog_id = _sanitize_id_segment(dog_id)
    _require_dog_access(user_id, dog_id)
    rows = list_voice_commands(user_id, dog_id)
    return [_to_response(r) for r in rows]


@router.post("")
async def upload_command(
    current_user: Annotated[dict, Depends(get_current_user)],
    file: UploadFile = File(...),
    dog_id: str = Form(...),
    command_id: str = Form(...),
):
    """Upload (or replace) a voice command WAV for a dog.

    Pushes voice_command_updated to the user's robot(s) over WS.
    """
    user_id = current_user["user_id"]
    dog_id = _sanitize_id_segment(dog_id)
    command_id = _sanitize_id_segment(command_id)
    _require_dog_access(user_id, dog_id)

    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max: {MAX_FILE_SIZE // (1024 * 1024)}MB",
        )

    target = _file_path(user_id, dog_id, command_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "wb") as f:
            f.write(content)
    except Exception as e:
        logger.error(f"[VOICE-CMD] write failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save voice command")

    row = upsert_voice_command(
        user_id=user_id,
        dog_id=dog_id,
        command_id=command_id,
        file_path=str(target),
        format="wav",
        size_bytes=size,
    )

    audio_url = _audio_url(user_id, dog_id, command_id)
    logger.info(
        f"[VOICE-CMD] Stored {target} ({size//1024}KB) for user={user_id} "
        f"dog={dog_id} command={command_id}"
    )

    await _push_to_robots(user_id, {
        "type": "voice_command_updated",
        "dog_id": dog_id,
        "command_id": command_id,
        "audio_url": audio_url,
        "updated_at": row["updated_at"],
    })

    return {"audio_url": audio_url, "updated_at": row["updated_at"]}


@router.delete("/{dog_id}/{command_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_command(
    dog_id: str,
    command_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a voice command. Pushes voice_command_deleted to the user's robot(s)."""
    user_id = current_user["user_id"]
    dog_id = _sanitize_id_segment(dog_id)
    command_id = _sanitize_id_segment(command_id)
    _require_dog_access(user_id, dog_id)

    deleted = delete_voice_command(user_id, dog_id, command_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice command not found")

    # Best-effort filesystem cleanup
    try:
        target = _file_path(user_id, dog_id, command_id)
        if target.exists():
            target.unlink()
    except Exception as e:
        logger.warning(f"[VOICE-CMD] failed to remove file: {e}")

    await _push_to_robots(user_id, {
        "type": "voice_command_deleted",
        "dog_id": dog_id,
        "command_id": command_id,
    })


@router.get("/file/{user_id}/{dog_id}/{command_id}")
async def serve_command(user_id: str, dog_id: str, command_id: str):
    """Serve the WAV file. Both app and robot fetch via this URL.

    Authentication: this URL is unauthenticated by design so the robot can
    download it without a JWT — file paths embed user_id/dog_id/command_id and
    the path is non-guessable for any practical purpose; if stronger auth is
    required later we can sign URLs at upload time.
    """
    user_id = _sanitize_id_segment(user_id)
    dog_id = _sanitize_id_segment(dog_id)
    command_id = _sanitize_id_segment(command_id)

    row = get_voice_command(user_id, dog_id, command_id)
    if not row:
        raise HTTPException(status_code=404, detail="Voice command not found")

    # Rows written before the persistent-storage move carry stale /tmp paths;
    # the canonical location is computed from the current storage root.
    target = _file_path(user_id, dog_id, command_id)
    if not target.is_file():
        target = Path(row["file_path"])
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Voice command file missing")

    return FileResponse(
        path=str(target),
        media_type="audio/wav",
        filename=f"{command_id}.wav",
    )
