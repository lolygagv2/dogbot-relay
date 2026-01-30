# Build 31 - Relay Claude Instructions

**Date:** January 30, 2026  
**Focus:** Fix TURN credential issues and ensure mission events flow through

---

## Context

Build 31 introduces a rewritten mission engine on the robot that broadcasts real-time `mission_progress` events. The relay server needs to:
1. Forward these new events to connected apps
2. Fix TURN credential authentication failures
3. Ensure WebRTC video works reliably for remote users

---

## Priority 1: TURN Credential Fix (CRITICAL)

### Problem

Robot logs show repeated TURN authentication failures:
```
aioice.stun.TransactionFailed: STUN transaction failed (401 - )
```

This breaks video streaming for remote users when P2P isn't possible (most real-world scenarios).

### Root Cause Options

1. **Credentials expiring too fast** - TTL too short
2. **Credentials not being refreshed** - Robot/app using stale creds
3. **Cloudflare rate limiting** - Too many credential requests
4. **Credential format mismatch** - Username/password encoding issue

### Required Fixes

#### 1.1 Extend Credential TTL

Current TTL might be too short. Extend to 24 hours minimum:

```python
# In turn_credentials.py or wherever credentials are generated

def generate_turn_credentials(device_id: str) -> dict:
    """Generate TURN credentials with sufficient TTL."""
    
    # Cloudflare TURN uses time-limited credentials
    # Format: timestamp:device_id
    ttl_seconds = 86400  # 24 hours (was probably shorter)
    expires_at = int(time.time()) + ttl_seconds
    
    username = f"{expires_at}:{device_id}"
    
    # HMAC-SHA1 of username with shared secret
    credential = hmac.new(
        CLOUDFLARE_TURN_SECRET.encode(),
        username.encode(),
        hashlib.sha1
    ).digest()
    credential_b64 = base64.b64encode(credential).decode()
    
    return {
        "urls": [
            "turn:turn.cloudflare.com:3478?transport=udp",
            "turn:turn.cloudflare.com:3478?transport=tcp",
            "turns:turn.cloudflare.com:5349?transport=tcp"
        ],
        "username": username,
        "credential": credential_b64,
        "ttl": ttl_seconds,
        "expires_at": expires_at
    }
```

#### 1.2 Add Credential Refresh Endpoint

Apps and robots should be able to request fresh credentials:

```python
# In routers/turn.py or similar

@router.get("/api/turn/credentials")
async def get_turn_credentials(
    device_id: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    """
    Get fresh TURN credentials for WebRTC.
    Call this before starting a new WebRTC session.
    """
    credentials = generate_turn_credentials(device_id)
    
    logger.info(f"[TURN] Generated credentials for {device_id}, expires in {credentials['ttl']}s")
    
    return credentials
```

#### 1.3 Include Credentials in WebRTC Signaling

When relay receives `webrtc_request`, include fresh credentials:

```python
async def handle_webrtc_request(websocket, message, device_id):
    """Handle WebRTC session request from app."""
    
    # Generate fresh credentials
    credentials = generate_turn_credentials(device_id)
    
    # Forward to robot WITH credentials
    robot_message = {
        "type": "webrtc_request",
        "from_user": message.get("user_id"),
        "turn_credentials": credentials  # Include this!
    }
    
    await send_to_robot(device_id, robot_message)
    
    # Also send credentials back to app
    await websocket.send_json({
        "type": "webrtc_credentials",
        "credentials": credentials
    })
```

---

## Priority 2: Forward New WebSocket Events

### New Events from Robot (Build 31)

The robot now sends these events that must be forwarded to connected apps:

| Event | Description |
|-------|-------------|
| `mission_progress` | Real-time mission state updates |
| `mode_changed` | Mode changes with lock status |
| `bark_detected` | Dog bark detected |
| `dog_detected` | Dog appeared in frame |
| `treat_dispensed` | Treat was given |

### Event Forwarding Logic

These should already work if relay is forwarding all events, but verify:

```python
async def handle_robot_message(device_id: str, message: dict):
    """Handle message from robot, forward to connected apps."""
    
    msg_type = message.get("type")
    
    # Events to forward to app
    forward_events = [
        "mission_progress",
        "mode_changed", 
        "bark_detected",
        "dog_detected",
        "treat_dispensed",
        "battery",
        "status_update",
        "alert",
        "photo",
        "audio_message",
        "webrtc_offer",
        "webrtc_ice"
    ]
    
    if msg_type == "event" or message.get("event") in forward_events:
        # Forward to all apps connected to this device
        await broadcast_to_apps(device_id, message)
        logger.debug(f"[RELAY] Forwarded {msg_type} to apps for {device_id}")
```

### Verify Event Flow

Add logging to confirm events are flowing:

```python
# Log all mission_progress events
if message.get("event") == "mission_progress":
    logger.info(f"[MISSION] Progress event: {message.get('data', {}).get('status')} "
                f"stage {message.get('data', {}).get('stage')}/{message.get('data', {}).get('total_stages')}")
```

---

## Priority 3: Forward New REST API Calls (If Proxying)

If the relay proxies REST calls to the robot (for remote access), ensure these new endpoints are forwarded:

### Mission Endpoints
```
GET  /missions/available
POST /missions/start
GET  /missions/status
POST /missions/stop
POST /missions/pause
POST /missions/resume
```

### Program Endpoints
```
GET    /programs/available
POST   /programs/start
GET    /programs/status
POST   /programs/stop
POST   /programs/pause
POST   /programs/resume
POST   /programs/create
DELETE /programs/{name}
```

### Report Endpoints
```
GET /reports/weekly
GET /reports/dog/{dog_id}
GET /reports/trends
GET /reports/compare
```

### Mode Endpoints
```
GET  /mode
POST /mode
```

If using a generic proxy, these should work automatically. If whitelisting endpoints, add these to the allowed list.

---

## Priority 4: Connection Stability

### 4.1 Heartbeat/Keepalive

Ensure WebSocket connections stay alive:

```python
# Send ping every 30 seconds
async def keepalive_task(websocket):
    while True:
        try:
            await asyncio.sleep(30)
            await websocket.ping()
        except:
            break
```

### 4.2 Reconnection Handling

When robot reconnects, ensure app is notified:

```python
async def on_robot_connect(device_id: str):
    """Called when robot connects/reconnects."""
    
    # Notify connected apps
    await broadcast_to_apps(device_id, {
        "type": "event",
        "event": "robot_connected",
        "device_id": device_id,
        "timestamp": datetime.utcnow().isoformat()
    })

async def on_robot_disconnect(device_id: str):
    """Called when robot disconnects."""
    
    await broadcast_to_apps(device_id, {
        "type": "event",
        "event": "robot_disconnected",
        "device_id": device_id,
        "timestamp": datetime.utcnow().isoformat()
    })
```

---

## Testing Checklist

### TURN Credentials
- [ ] Generate credentials with 24-hour TTL
- [ ] `/api/turn/credentials` endpoint returns valid credentials
- [ ] WebRTC request includes credentials for both robot and app
- [ ] No more 401 STUN errors in robot logs

### Event Forwarding
- [ ] Start mission on robot → app receives `mission_progress` events
- [ ] Mission state changes flow through (waiting → greeting → command → watching → success)
- [ ] Mode lock status included in `mode_changed` events
- [ ] `treat_dispensed` events reach app

### Connection Stability
- [ ] WebSocket stays connected for 30+ minutes
- [ ] Robot reconnect triggers `robot_connected` event to app
- [ ] No message drops during active mission

---

## Quick Diagnostic Commands

```bash
# Check relay logs for TURN issues
journalctl -u wimz-relay --since "10 minutes ago" | grep -i turn

# Check for mission events flowing
journalctl -u wimz-relay --since "10 minutes ago" | grep -i mission

# Check WebSocket connections
journalctl -u wimz-relay --since "10 minutes ago" | grep -i "connect\|disconnect"
```

---

## Files Likely Needing Changes

1. `app/routers/turn.py` or `app/turn_credentials.py` - Credential generation
2. `app/routers/websocket.py` - Event forwarding
3. `app/connection_manager.py` - Connection tracking
4. `app/config.py` - TTL settings

---

*Build 31 - Ensure video works and mission events flow*
