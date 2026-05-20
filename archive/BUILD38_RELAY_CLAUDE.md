# Build 38 — RELAY CLAUDE Instructions

**Date:** February 1, 2026
**Priority:** HTTP music upload endpoint. Everything else is minor.

---

## Architecture Context

Build 38 makes these architecture changes that affect the relay:

1. **File transfers use HTTP, not WebSocket.** MP3 uploads go App → Relay (HTTP multipart) → Relay stores file → Relay tells Robot to fetch via HTTP.
2. **Schedules live on the robot, not the relay.** Relay schedule endpoints should be deprecated or proxy to robot. Do NOT store schedules in relay SQLite going forward.
3. **Relay is a message router.** It forwards commands and events. It does not own state except for authentication and file staging.

---

## P1-L1: HTTP Music Upload Endpoint (PRIMARY TASK)

**Why:** The 5MB base64 MP3 over WebSocket crashes the robot's connection. App will switch to HTTP multipart upload. Relay needs to receive, stage the file, and tell the robot to fetch it.

**New endpoint:**
```
POST /api/music/upload
Content-Type: multipart/form-data
Authorization: Bearer <token>

Fields:
  - dog_id: string (required)
  - device_id: string (required, which robot to send to)
File:
  - file: MP3 file (max 10MB)
```

**Implementation:**

```python
import uuid
import os
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import FileResponse

router = APIRouter()

UPLOAD_DIR = "/tmp/wimz-uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.post("/api/music/upload")
async def upload_music(
    file: UploadFile = File(...),
    dog_id: str = Form(...),
    device_id: str = Form(...),
    user = Depends(get_current_user)
):
    # Validate file
    if not file.filename.endswith(('.mp3', '.wav', '.m4a', '.aac')):
        raise HTTPException(400, "Only audio files accepted")
    
    if file.size and file.size > 10 * 1024 * 1024:  # 10MB
        raise HTTPException(413, "File too large (max 10MB)")
    
    # Stage file with unique ID
    file_id = str(uuid.uuid4())
    staged_path = os.path.join(UPLOAD_DIR, f"{file_id}_{file.filename}")
    
    with open(staged_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    # Tell robot to download the file
    download_url = f"https://api.wimzai.com/api/music/file/{file_id}"
    
    await manager.send_to_device(device_id, {
        "type": "command",
        "command": "download_song",
        "data": {
            "url": download_url,
            "filename": file.filename,
            "dog_id": dog_id
        }
    })
    
    return {"status": "ok", "file_id": file_id, "message": "Upload staged, robot notified"}


@router.get("/api/music/file/{file_id}")
async def serve_music_file(file_id: str):
    """Robot calls this to download the staged file."""
    # Find the file matching this ID
    for fname in os.listdir(UPLOAD_DIR):
        if fname.startswith(file_id):
            return FileResponse(
                os.path.join(UPLOAD_DIR, fname),
                media_type="audio/mpeg"
            )
    raise HTTPException(404, "File not found or expired")
```

**Cleanup:** Add a background task or cron to delete files older than 1 hour from `/tmp/wimz-uploads/`.

**Security:** The `/api/music/file/{file_id}` endpoint uses UUID file IDs which are unguessable, but optionally add robot auth header check if concerned.

---

## P1-L2: Schedule Endpoints — Deprecation Notice

**Architecture change:** Schedules now live on the robot, not the relay.

**Options (pick one):**

**A) Remove relay schedule endpoints entirely.** App sends schedule commands to robot via WebSocket. Simplest approach.

**B) Keep relay endpoints as passthrough/proxy.** App calls relay REST → relay forwards to robot via WebSocket → returns response. More work but lets the app use REST for CRUD.

**Recommendation:** Option A. The app already sends all other commands via WebSocket. Schedules should be the same. If the relay schedule endpoints in `app/routers/schedule.py` stay, add a deprecation log warning.

**Either way:** Do NOT store new schedules in relay SQLite. The robot is the source of truth for schedules.

---

## P2-L3: Large Message Safety

Even after MP3 moves to HTTP, add a safety valve for the future:

```python
MAX_WEBSOCKET_MESSAGE_SIZE = 1_000_000  # 1MB

async def on_message(self, websocket, data):
    if len(data) > MAX_WEBSOCKET_MESSAGE_SIZE:
        logger.warning(f"[REJECTED] Message too large: {len(data)} bytes from {websocket.client}")
        await websocket.send_json({
            "type": "error",
            "message": "Message too large. Use HTTP upload for files over 1MB."
        })
        return
    # ... process normally
```

---

## P2-L4: Forwarding Verification

Verify these event types are properly forwarded from robot to app (they should be already, but confirm):

- `mission_progress` — Mission state updates
- `mode_changed` — Mode transitions
- `detection` — Dog detection events
- `upload_complete` / `upload_error` — File upload results
- `schedule_created` / `schedule_updated` / `schedule_deleted` — Schedule confirmations

If any are missing from the WebSocket router's forwarding logic, add them.

---

## Test Checklist

- [ ] `POST /api/music/upload` accepts multipart MP3, stores file, returns success
- [ ] `GET /api/music/file/{file_id}` serves the staged file to robot
- [ ] Robot receives `download_song` WebSocket command after app uploads
- [ ] Files older than 1 hour cleaned up
- [ ] Large WebSocket messages (>1MB) rejected with error message
- [ ] All robot events (mission_progress, mode_changed, detection) forwarded to app
