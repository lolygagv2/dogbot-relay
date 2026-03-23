"""
Media Upload Router (Build 49)

HTTP endpoints for robot video uploads. Robot POSTs recorded video here,
relay stores it temporarily, app downloads via the returned URL.

Flow:
  1. Robot POST /api/media/upload (multipart: file + device_id)
  2. Relay stores file, returns download_url
  3. Robot sends video_ready event to app with download_url
  4. App GET /api/media/download/{filename} to fetch the MP4
"""
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["Media"])

# Storage directory
MEDIA_DIR = Path("/tmp/wimz-media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Max file size: 100MB (video files can be large)
MAX_FILE_SIZE = 100 * 1024 * 1024

# File expiry: 1 hour
FILE_EXPIRY_SECONDS = 3600


def cleanup_old_files():
    """Remove files older than FILE_EXPIRY_SECONDS."""
    try:
        now = time.time()
        count = 0
        for filepath in MEDIA_DIR.iterdir():
            if filepath.is_file() and (now - filepath.stat().st_mtime) > FILE_EXPIRY_SECONDS:
                filepath.unlink()
                count += 1
                logger.info(f"[MEDIA-CLEANUP] Deleted expired: {filepath.name}")
        if count > 0:
            logger.info(f"[MEDIA-CLEANUP] Removed {count} expired file(s)")
    except Exception as e:
        logger.error(f"[MEDIA-CLEANUP] Error: {e}")


@router.post("/upload")
async def upload_media(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    device_id: str = Form(...),
):
    """
    Robot uploads a video file. No JWT required (robot uses device auth).

    Returns download_url for the app to fetch the file.
    """
    # Read file content
    content = await file.read()
    file_size = len(content)

    if file_size == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max: {MAX_FILE_SIZE // (1024 * 1024)}MB"
        )

    # Determine extension
    filename = file.filename or "video.mp4"
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "mp4"
    # Sanitize extension
    ext = "".join(c for c in ext if c.isalnum())[:10] or "mp4"

    stored_name = f"{device_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = MEDIA_DIR / stored_name

    try:
        with open(filepath, "wb") as f:
            f.write(content)
        logger.info(f"[MEDIA] Stored {stored_name} ({file_size // 1024}KB) from device {device_id}")
    except Exception as e:
        logger.error(f"[MEDIA] Failed to write file: {e}")
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Schedule cleanup of old files
    background_tasks.add_task(cleanup_old_files)

    return {
        "download_url": f"/api/media/download/{stored_name}",
        "filename": stored_name,
        "size": file_size,
    }


@router.get("/download/{filename}")
async def download_media(filename: str):
    """
    App downloads a stored media file.

    The filename is returned from the upload endpoint.
    Files auto-expire after 1 hour.
    """
    # Sanitize: prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = MEDIA_DIR / safe_name

    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired")

    logger.info(f"[MEDIA] Serving {safe_name} ({filepath.stat().st_size // 1024}KB)")

    return FileResponse(
        path=str(filepath),
        media_type="video/mp4",
        filename=safe_name,
    )
