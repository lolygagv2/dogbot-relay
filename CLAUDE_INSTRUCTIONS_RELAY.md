# Claude Code Instructions: WIM-Z Relay Server WebRTC Integration

## Context

You are working on the WIM-Z cloud relay server (FastAPI). The relay needs to:
1. Generate Cloudflare TURN credentials for WebRTC connections
2. Route WebRTC signaling messages between app and robot
3. All actual video goes peer-to-peer or through Cloudflare TURN - NOT through this server

**Read API_CONTRACT.md first** - it contains the complete specification.

## Your Task

Add WebRTC signaling support to the existing relay server:
1. Add endpoint to generate Cloudflare TURN credentials
2. Route WebRTC signaling messages (offer, answer, ICE candidates)
3. Track active WebRTC sessions

## Files to Create/Modify

### 1. Create: `app/services/turn_service.py`

```python
import httpx
from typing import Optional
import os

class TURNService:
    """Generate short-lived TURN credentials from Cloudflare."""
    
    def __init__(self):
        self.turn_key_id = os.getenv("CLOUDFLARE_TURN_KEY_ID")
        self.turn_api_token = os.getenv("CLOUDFLARE_TURN_API_TOKEN")
        self.base_url = "https://rtc.live.cloudflare.com/v1/turn/keys"
    
    async def generate_credentials(self, ttl: int = 3600) -> dict:
        """
        Generate short-lived TURN credentials.
        
        Args:
            ttl: Time-to-live in seconds (default 1 hour)
            
        Returns:
            dict with iceServers array for RTCPeerConnection
        """
        if not self.turn_key_id or not self.turn_api_token:
            raise ValueError("Cloudflare TURN credentials not configured")
        
        url = f"{self.base_url}/{self.turn_key_id}/credentials/generate-ice-servers"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.turn_api_token}",
                    "Content-Type": "application/json"
                },
                json={"ttl": ttl}
            )
            response.raise_for_status()
            return response.json()

# Singleton instance
turn_service = TURNService()
```

### 2. Create: `app/routers/turn.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from ..services.turn_service import turn_service
from ..auth import get_current_user

router = APIRouter(prefix="/api/turn", tags=["turn"])

class CredentialRequest(BaseModel):
    ttl: Optional[int] = 3600

@router.post("/credentials")
async def generate_turn_credentials(
    request: CredentialRequest,
    user = Depends(get_current_user)
):
    """Generate short-lived TURN credentials for WebRTC."""
    try:
        credentials = await turn_service.generate_credentials(request.ttl)
        return credentials
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cloudflare API error: {str(e)}")
```

### 3. Modify: `app/main.py`

Add the router:
```python
from .routers import turn

app.include_router(turn.router)
```

### 4. Modify: `app/websocket/handler.py` (or equivalent)

Add WebRTC signaling message handling:

```python
import uuid

# Track active WebRTC sessions
webrtc_sessions = {}  # session_id -> {"app_ws": ws, "device_id": str}

async def handle_websocket_message(websocket, message, connection_type, identifier):
    msg_type = message.get("type")
    
    # ... existing handlers ...
    
    # WebRTC Signaling
    if msg_type == "webrtc_request":
        await handle_webrtc_request(websocket, message, identifier)
    
    elif msg_type == "webrtc_offer":
        await forward_webrtc_to_app(message)
    
    elif msg_type == "webrtc_answer":
        await forward_webrtc_to_robot(message)
    
    elif msg_type == "webrtc_ice":
        await forward_ice_candidate(message, connection_type)
    
    elif msg_type == "webrtc_close":
        await handle_webrtc_close(message)


async def handle_webrtc_request(websocket, message, user_id):
    """App requests video stream from robot."""
    device_id = message.get("device_id")
    
    # Verify user owns this device
    if not await user_owns_device(user_id, device_id):
        await websocket.send_json({"type": "error", "message": "Not authorized"})
        return
    
    # Check if robot is online
    robot_ws = get_device_websocket(device_id)
    if not robot_ws:
        await websocket.send_json({"type": "error", "message": "Device offline"})
        return
    
    # Generate session ID
    session_id = str(uuid.uuid4())
    
    # Generate TURN credentials
    try:
        ice_servers = await turn_service.generate_credentials(ttl=3600)
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"TURN error: {e}"})
        return
    
    # Track session
    webrtc_sessions[session_id] = {
        "app_ws": websocket,
        "device_id": device_id,
        "user_id": user_id
    }
    
    # Send credentials to app
    await websocket.send_json({
        "type": "webrtc_credentials",
        "session_id": session_id,
        "ice_servers": ice_servers
    })
    
    # Forward request to robot with credentials
    await robot_ws.send_json({
        "type": "webrtc_request",
        "session_id": session_id,
        "ice_servers": ice_servers
    })


async def forward_webrtc_to_app(message):
    """Forward offer/ICE from robot to app."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)
    
    if session and session["app_ws"]:
        await session["app_ws"].send_json(message)


async def forward_webrtc_to_robot(message):
    """Forward answer/ICE from app to robot."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)
    
    if session:
        device_id = session["device_id"]
        robot_ws = get_device_websocket(device_id)
        if robot_ws:
            await robot_ws.send_json(message)


async def forward_ice_candidate(message, from_type):
    """Forward ICE candidate to the other peer."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)
    
    if not session:
        return
    
    if from_type == "device":
        # From robot, forward to app
        if session["app_ws"]:
            await session["app_ws"].send_json(message)
    else:
        # From app, forward to robot
        device_id = session["device_id"]
        robot_ws = get_device_websocket(device_id)
        if robot_ws:
            await robot_ws.send_json(message)


async def handle_webrtc_close(message):
    """Clean up WebRTC session."""
    session_id = message.get("session_id")
    session = webrtc_sessions.pop(session_id, None)
    
    if session:
        # Notify both parties
        if session["app_ws"]:
            try:
                await session["app_ws"].send_json({"type": "webrtc_close", "session_id": session_id})
            except:
                pass
        
        robot_ws = get_device_websocket(session["device_id"])
        if robot_ws:
            try:
                await robot_ws.send_json({"type": "webrtc_close", "session_id": session_id})
            except:
                pass
```

### 5. Update: `.env.example`

Add:
```bash
# Cloudflare Calls TURN Service
CLOUDFLARE_TURN_KEY_ID=your_turn_key_id_here
CLOUDFLARE_TURN_API_TOKEN=your_turn_api_token_here
```

### 6. Update: `requirements.txt`

Add:
```
httpx>=0.25.0
```

## Environment Setup (for production)

1. Go to Cloudflare Dashboard → Calls
2. Create a new TURN App
3. Copy the Turn Token ID and API Token
4. Add to your .env file

## Testing

### Test TURN credential generation:
```bash
curl -X POST http://localhost:8000/api/turn/credentials \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ttl": 3600}'
```

Expected response:
```json
{
  "iceServers": [
    {"urls": ["stun:stun.cloudflare.com:3478"]},
    {
      "urls": ["turn:turn.cloudflare.com:3478?transport=udp", ...],
      "username": "...",
      "credential": "..."
    }
  ]
}
```

### Test WebSocket signaling:
1. Connect as app: `ws://localhost:8000/ws/app?token=JWT`
2. Connect as device: `ws://localhost:8000/ws/device?device_id=X&sig=Y`
3. App sends: `{"type": "webrtc_request", "device_id": "X"}`
4. Verify device receives request with ice_servers
5. Device sends offer, verify app receives it
6. App sends answer, verify device receives it

## Success Criteria

- [ ] POST /api/turn/credentials returns valid Cloudflare ice_servers
- [ ] webrtc_request generates session and forwards to robot
- [ ] webrtc_offer routes from robot to app
- [ ] webrtc_answer routes from app to robot  
- [ ] webrtc_ice routes bidirectionally
- [ ] webrtc_close cleans up session
- [ ] Sessions are tracked and cleaned up properly
- [ ] Errors are handled gracefully
