"""
Music Upload Router (Build 38)

HTTP endpoints for music file uploads. Files are staged on the relay
and the robot is notified to download them via HTTP.

This replaces the WebSocket-based upload which crashed robots due to
large base64 payloads.
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse

from app.auth import get_current_user
from app.connection_manager import get_connection_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/music", tags=["Music"])

# Upload directory for staged files
UPLOAD_DIR = Path("/tmp/wimz-uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Max file size: 10MB
MAX_FILE_SIZE = 10 * 1024 * 1024

# File expiry: 1 hour
FILE_EXPIRY_SECONDS = 3600

# Allowed audio extensions
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}


async def cleanup_old_files():
    """Remove files older than FILE_EXPIRY_SECONDS."""
    try:
        now = datetime.now()
        count = 0
        for filepath in UPLOAD_DIR.iterdir():
            if filepath.is_file():
                file_age = now - datetime.fromtimestamp(filepath.stat().st_mtime)
                if file_age.total_seconds() > FILE_EXPIRY_SECONDS:
                    filepath.unlink()
                    count += 1
                    logger.info(f"[CLEANUP] Deleted expired file: {filepath.name}")
        if count > 0:
            logger.info(f"[CLEANUP] Removed {count} expired file(s)")
    except Exception as e:
        logger.error(f"[CLEANUP] Error cleaning up files: {e}")


@router.post("/upload")
async def upload_music(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    dog_id: str = Form(...),
    device_id: str = Form(...),
    user: dict = Depends(get_current_user)
):
    """
    Upload a music file for a dog.

    The file is staged on the relay server and the robot is notified
    to download it via HTTP GET.

    - file: Audio file (MP3, WAV, M4A, AAC, OGG) max 10MB
    - dog_id: Which dog this audio is for
    - device_id: Which robot should receive the file
    """
    user_id = user.get("user_id")

    # Validate file extension
    filename = file.filename or "audio.mp3"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate file size
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size: {MAX_FILE_SIZE // (1024*1024)}MB"
        )

    if file_size == 0:
        raise HTTPException(
            status_code=400,
            detail="Empty file"
        )

    # Generate unique file ID and save
    file_id = str(uuid.uuid4())
    # Sanitize filename - keep only alphanumeric, dash, underscore, dot
    safe_filename = "".join(c for c in filename if c.isalnum() or c in ".-_")
    staged_path = UPLOAD_DIR / f"{file_id}_{safe_filename}"

    try:
        with open(staged_path, "wb") as f:
            f.write(content)
        logger.info(f"[UPLOAD] Staged file: {staged_path.name} ({file_size // 1024}KB) for dog {dog_id}")
    except Exception as e:
        logger.error(f"[UPLOAD] Failed to write file: {e}")
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Get connection manager and verify device ownership
    manager = get_connection_manager()

    owner = manager.get_device_owner(device_id)
    if owner != user_id:
        # Clean up staged file
        staged_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=403,
            detail="Not authorized to upload to this device"
        )

    # Check if robot is online
    if not manager.is_robot_online(device_id):
        # Keep the file staged - robot can fetch when it comes online
        logger.warning(f"[UPLOAD] Robot {device_id} is offline, file staged for later")

    # Send download command to robot
    # The robot will call GET /api/music/file/{file_id} to download
    download_url = f"/api/music/file/{file_id}"

    await manager.send_to_robot(device_id, {
        "type": "command",
        "command": "download_song",
        "data": {
            "url": download_url,
            "file_id": file_id,
            "filename": filename,
            "dog_id": dog_id,
            "size": file_size
        }
    })

    logger.info(f"[UPLOAD] Notified robot {device_id} to download file {file_id}")

    # Schedule cleanup of old files
    background_tasks.add_task(cleanup_old_files)

    return {
        "status": "ok",
        "file_id": file_id,
        "filename": filename,
        "size": file_size,
        "message": "Upload staged, robot notified to download"
    }


@router.get("/file/{file_id}")
async def serve_music_file(file_id: str):
    """
    Serve a staged music file for robot download.

    The file_id is a UUID that was returned from the upload endpoint.
    Files are automatically deleted after 1 hour.
    """
    # Validate file_id format (UUID)
    try:
        uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID format")

    # Find the file matching this ID
    for filepath in UPLOAD_DIR.iterdir():
        if filepath.is_file() and filepath.name.startswith(file_id):
            logger.info(f"[DOWNLOAD] Serving file: {filepath.name}")

            # Determine media type from extension
            ext = filepath.suffix.lower()
            media_types = {
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".m4a": "audio/mp4",
                ".aac": "audio/aac",
                ".ogg": "audio/ogg"
            }
            media_type = media_types.get(ext, "audio/mpeg")

            return FileResponse(
                path=str(filepath),
                media_type=media_type,
                filename=filepath.name.split("_", 1)[1] if "_" in filepath.name else filepath.name
            )

    raise HTTPException(status_code=404, detail="File not found or expired")


@router.delete("/file/{file_id}")
async def delete_music_file(
    file_id: str,
    user: dict = Depends(get_current_user)
):
    """
    Delete a staged music file.

    Called after robot confirms download, or to cancel an upload.
    """
    # Validate file_id format
    try:
        uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID format")

    # Find and delete the file
    for filepath in UPLOAD_DIR.iterdir():
        if filepath.is_file() and filepath.name.startswith(file_id):
            filepath.unlink()
            logger.info(f"[DELETE] Removed file: {filepath.name}")
            return {"status": "ok", "message": "File deleted"}

    raise HTTPException(status_code=404, detail="File not found")
