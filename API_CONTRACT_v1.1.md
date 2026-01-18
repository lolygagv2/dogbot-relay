# WIM-Z API Contract v1.1

> **Single source of truth** for Robot, Mobile App, and Cloud Relay Server.
> When changing any API, update this file FIRST, then update each project to match.

Last Updated: 2025-01-18
Version: 1.1 - Added WebRTC/Cloudflare Calls integration

---

## Architecture Overview

```
┌─────────────────┐                                    ┌─────────────────┐
│   Mobile App    │◄──── WebRTC Video (P2P/TURN) ────►│   WIM-Z Robot   │
│   (Flutter)     │                                    │  (Raspberry Pi) │
└────────┬────────┘                                    └────────┬────────┘
         │                                                      │
         │  Commands/Events                    Commands/Events  │
         │  (WebSocket)                           (WebSocket)   │
         │                                                      │
         └──────────────►┌─────────────────┐◄───────────────────┘
                         │  Cloud Relay    │
                         │  (FastAPI)      │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────┐
                         │ Cloudflare TURN │
                         │ (Credential Gen)│
                         └─────────────────┘
```

**Data Flow:**
- **Commands/Events**: App ↔ Relay ↔ Robot (WebSocket, low bandwidth)
- **Video**: App ↔ Robot direct via WebRTC (high bandwidth, P2P when possible)
- **TURN Relay**: Cloudflare handles NAT traversal when P2P fails

---

## Configuration & Secrets

### Environment Variables

**Robot (.env)**
```bash
# Relay Server
RELAY_URL=wss://api.wimz.io/ws/device
DEVICE_ID=wimz_abc123
DEVICE_SECRET=your_device_secret_here

# Cloudflare (received from relay, don't hardcode)
# ICE servers are fetched dynamically
```

**Relay Server (.env)**
```bash
# Server
HOST=0.0.0.0
PORT=8000
JWT_SECRET=your_jwt_secret_here

# Database
DATABASE_URL=postgresql://user:pass@host:5432/wimz

# Cloudflare Calls TURN
CLOUDFLARE_TURN_KEY_ID=your_turn_key_id
CLOUDFLARE_TURN_API_TOKEN=your_turn_api_token
```

**Mobile App (config)**
```dart
// lib/core/config/environment.dart
class CloudConfig {
  static const relayUrl = 'wss://api.wimz.io';
  // ICE servers fetched from relay at runtime
}
```

---

## Cloudflare TURN Integration

### Credential Flow

```
1. App connects to Relay
2. App requests TURN credentials from Relay
3. Relay calls Cloudflare API to generate short-lived credentials
4. Relay returns iceServers config to App
5. App uses iceServers when creating RTCPeerConnection
6. Robot receives same iceServers via Relay
7. WebRTC connection established through Cloudflare's global network
```

### Relay Server: Generate TURN Credentials

**Endpoint:** `POST /api/turn/credentials`

**Request:**
```json
{
  "ttl": 86400
}
```

**Implementation (Relay Server):**
```python
import httpx

async def generate_turn_credentials(ttl: int = 86400) -> dict:
    """Generate short-lived TURN credentials from Cloudflare."""
    url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{TURN_KEY_ID}/credentials/generate-ice-servers"
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {TURN_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json={"ttl": ttl}
        )
        return response.json()
```

**Response:**
```json
{
  "iceServers": [
    {
      "urls": [
        "stun:stun.cloudflare.com:3478"
      ]
    },
    {
      "urls": [
        "turn:turn.cloudflare.com:3478?transport=udp",
        "turn:turn.cloudflare.com:3478?transport=tcp",
        "turns:turn.cloudflare.com:443?transport=tcp"
      ],
      "username": "generated_username_hash",
      "credential": "generated_credential_hash"
    }
  ]
}
```

---

## WebRTC Signaling API

### Overview

WebRTC requires signaling to exchange connection information (SDP offers/answers, ICE candidates). The Relay Server acts as the signaling server.

### Signaling Flow

```
┌─────────┐         ┌─────────┐         ┌─────────┐
│   App   │         │  Relay  │         │  Robot  │
└────┬────┘         └────┬────┘         └────┬────┘
     │                   │                   │
     │ 1. Request TURN   │                   │
     │   credentials     │                   │
     │──────────────────>│                   │
     │                   │                   │
     │ 2. Return         │                   │
     │   iceServers      │                   │
     │<──────────────────│                   │
     │                   │                   │
     │ 3. webrtc_request │                   │
     │   (initiate)      │                   │
     │──────────────────>│ 4. webrtc_request │
     │                   │   + iceServers    │
     │                   │──────────────────>│
     │                   │                   │
     │                   │ 5. webrtc_offer   │
     │ 6. webrtc_offer   │   (SDP)           │
     │<──────────────────│<──────────────────│
     │                   │                   │
     │ 7. webrtc_answer  │                   │
     │   (SDP)           │                   │
     │──────────────────>│ 8. webrtc_answer  │
     │                   │──────────────────>│
     │                   │                   │
     │ 9. webrtc_ice     │ 10. webrtc_ice    │
     │<─────────────────>│<─────────────────>│
     │   (candidates)    │   (candidates)    │
     │                   │                   │
     │         WebRTC Connection Established │
     │◄═══════════════════════════════════►│
     │              (Video Stream)           │
```

### WebSocket Signaling Messages

#### App → Relay: Request Video Stream

```json
{
  "type": "webrtc_request",
  "device_id": "wimz_abc123"
}
```

#### Relay → Robot: Initiate Video Stream

```json
{
  "type": "webrtc_request",
  "session_id": "session_xyz",
  "ice_servers": {
    "iceServers": [...]
  }
}
```

#### Robot → Relay → App: SDP Offer

```json
{
  "type": "webrtc_offer",
  "session_id": "session_xyz",
  "sdp": {
    "type": "offer",
    "sdp": "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n..."
  }
}
```

#### App → Relay → Robot: SDP Answer

```json
{
  "type": "webrtc_answer",
  "session_id": "session_xyz",
  "sdp": {
    "type": "answer", 
    "sdp": "v=0\r\no=- 4611731400430051337 2 IN IP4 127.0.0.1\r\n..."
  }
}
```

#### Bidirectional: ICE Candidates

```json
{
  "type": "webrtc_ice",
  "session_id": "session_xyz",
  "candidate": {
    "candidate": "candidate:842163049 1 udp 1677729535 ...",
    "sdpMid": "0",
    "sdpMLineIndex": 0
  }
}
```

#### Close Video Stream

```json
{
  "type": "webrtc_close",
  "session_id": "session_xyz"
}
```

---

## Robot WebRTC Implementation

### Python (aiortc) Setup

**Dependencies:**
```bash
pip install aiortc aiohttp
sudo apt-get install libavdevice-dev libavfilter-dev libopus-dev libvpx-dev
```

**Video Track from Camera:**
```python
from aiortc import VideoStreamTrack
from aiortc.contrib.media import MediaPlayer
from av import VideoFrame
import numpy as np

class WIMZVideoTrack(VideoStreamTrack):
    """Video track that captures from Pi camera."""
    
    def __init__(self, detector_service):
        super().__init__()
        self.detector = detector_service
        
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Get frame from detector service (already has AI overlay)
        frame = self.detector.get_current_frame()
        
        # Convert to VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        
        return video_frame
```

**WebRTC Service:**
```python
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate

class WebRTCService:
    def __init__(self, detector_service):
        self.detector = detector_service
        self.connections = {}  # session_id -> RTCPeerConnection
        
    async def create_offer(self, session_id: str, ice_servers: dict) -> dict:
        """Create WebRTC offer with video track."""
        config = {"iceServers": ice_servers.get("iceServers", [])}
        pc = RTCPeerConnection(configuration=config)
        self.connections[session_id] = pc
        
        # Add video track
        video_track = WIMZVideoTrack(self.detector)
        pc.addTrack(video_track)
        
        # Create offer
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        
        return {
            "type": "offer",
            "sdp": pc.localDescription.sdp
        }
    
    async def handle_answer(self, session_id: str, sdp: dict):
        """Handle SDP answer from app."""
        pc = self.connections.get(session_id)
        if pc:
            answer = RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"])
            await pc.setRemoteDescription(answer)
    
    async def handle_ice_candidate(self, session_id: str, candidate: dict):
        """Handle ICE candidate from app."""
        pc = self.connections.get(session_id)
        if pc and candidate:
            ice = RTCIceCandidate(
                sdpMid=candidate.get("sdpMid"),
                sdpMLineIndex=candidate.get("sdpMLineIndex"),
                candidate=candidate.get("candidate")
            )
            await pc.addIceCandidate(ice)
    
    async def close(self, session_id: str):
        """Close WebRTC connection."""
        pc = self.connections.pop(session_id, None)
        if pc:
            await pc.close()
```

---

## Mobile App WebRTC Implementation

### Flutter (flutter_webrtc) Setup

**Dependencies (pubspec.yaml):**
```yaml
dependencies:
  flutter_webrtc: ^0.9.47
```

**WebRTC Service:**
```dart
import 'package:flutter_webrtc/flutter_webrtc.dart';

class WebRTCService {
  RTCPeerConnection? _peerConnection;
  RTCVideoRenderer remoteRenderer = RTCVideoRenderer();
  String? _sessionId;
  
  final Function(Map<String, dynamic>) onIceCandidate;
  final Function(Map<String, dynamic>) onOffer;
  
  WebRTCService({
    required this.onIceCandidate,
    required this.onOffer,
  });
  
  Future<void> initialize() async {
    await remoteRenderer.initialize();
  }
  
  Future<void> createConnection(Map<String, dynamic> iceServers, String sessionId) async {
    _sessionId = sessionId;
    
    final config = {
      'iceServers': iceServers['iceServers'],
      'sdpSemantics': 'unified-plan',
    };
    
    _peerConnection = await createPeerConnection(config);
    
    // Handle incoming video track
    _peerConnection!.onTrack = (RTCTrackEvent event) {
      if (event.track.kind == 'video') {
        remoteRenderer.srcObject = event.streams[0];
      }
    };
    
    // Handle ICE candidates
    _peerConnection!.onIceCandidate = (RTCIceCandidate candidate) {
      onIceCandidate({
        'type': 'webrtc_ice',
        'session_id': _sessionId,
        'candidate': {
          'candidate': candidate.candidate,
          'sdpMid': candidate.sdpMid,
          'sdpMLineIndex': candidate.sdpMLineIndex,
        },
      });
    };
  }
  
  Future<void> handleOffer(Map<String, dynamic> offer) async {
    final description = RTCSessionDescription(
      offer['sdp'],
      offer['type'],
    );
    await _peerConnection!.setRemoteDescription(description);
    
    // Create and send answer
    final answer = await _peerConnection!.createAnswer();
    await _peerConnection!.setLocalDescription(answer);
    
    // Send answer back through relay
    onOffer({
      'type': 'webrtc_answer',
      'session_id': _sessionId,
      'sdp': {
        'type': answer.type,
        'sdp': answer.sdp,
      },
    });
  }
  
  Future<void> handleIceCandidate(Map<String, dynamic> candidate) async {
    final iceCandidate = RTCIceCandidate(
      candidate['candidate'],
      candidate['sdpMid'],
      candidate['sdpMLineIndex'],
    );
    await _peerConnection!.addIceCandidate(iceCandidate);
  }
  
  Future<void> close() async {
    await _peerConnection?.close();
    _peerConnection = null;
  }
  
  void dispose() {
    close();
    remoteRenderer.dispose();
  }
}
```

**Video Widget:**
```dart
class WebRTCVideoView extends StatelessWidget {
  final RTCVideoRenderer renderer;
  
  const WebRTCVideoView({required this.renderer});
  
  @override
  Widget build(BuildContext context) {
    return RTCVideoView(
      renderer,
      objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover,
    );
  }
}
```

---

## Relay Server WebRTC Additions

### New Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/turn/credentials` | Generate Cloudflare TURN credentials |

### WebSocket Message Routing

Add these handlers to existing WebSocket router:

```python
# In websocket handler
async def handle_message(websocket, message, user_or_device):
    msg_type = message.get("type")
    
    # Existing handlers...
    
    # WebRTC signaling
    if msg_type == "webrtc_request":
        await handle_webrtc_request(websocket, message, user_or_device)
    elif msg_type == "webrtc_offer":
        await forward_to_app(message)
    elif msg_type == "webrtc_answer":
        await forward_to_robot(message)
    elif msg_type == "webrtc_ice":
        await forward_ice_candidate(message)
    elif msg_type == "webrtc_close":
        await forward_close(message)

async def handle_webrtc_request(websocket, message, user):
    """App requests video stream from robot."""
    device_id = message.get("device_id")
    
    # Generate session ID
    session_id = str(uuid.uuid4())
    
    # Get TURN credentials
    ice_servers = await generate_turn_credentials(ttl=3600)
    
    # Forward to robot with credentials
    await send_to_robot(device_id, {
        "type": "webrtc_request",
        "session_id": session_id,
        "ice_servers": ice_servers
    })
    
    # Also send credentials to app
    await websocket.send_json({
        "type": "webrtc_credentials",
        "session_id": session_id,
        "ice_servers": ice_servers
    })
```

---

## Video Stream Parameters

### Recommended Settings

| Parameter | Value | Notes |
|-----------|-------|-------|
| Resolution | 720p (1280x720) | Balance quality/bandwidth |
| FPS | 15 | Sufficient for monitoring, saves CPU |
| Bitrate | 1.5 Mbps | Adjust based on network |
| Codec | VP8 or H264 | H264 preferred for Pi hardware encoding |
| Audio | Disabled | Save bandwidth, add later if needed |

### Robot Config (robot_config.yaml)

```yaml
webrtc:
  enabled: true
  video:
    width: 1280
    height: 720
    fps: 15
    bitrate_kbps: 1500
    codec: "H264"  # or "VP8"
  audio:
    enabled: false
  max_connections: 2  # Limit concurrent viewers
```

---

## REST API Endpoints

*(Unchanged from v1.0 - see below)*

### Health & Status

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| GET | `/health` | Server alive check | - | `{"status": "ok"}` |
| GET | `/telemetry` | Full system status | - | `Telemetry` object |

### Motor Control

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| POST | `/motor/speed` | Set motor speeds | `{"left": float, "right": float}` | `{"success": true}` |
| POST | `/motor/stop` | Stop all motors | - | `{"success": true}` |
| POST | `/motor/emergency` | Emergency stop | - | `{"success": true}` |

### Camera & Servos

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| GET | `/camera/stream` | MJPEG video stream (local only) | - | `multipart/x-mixed-replace` |
| GET | `/camera/snapshot` | Single JPEG frame | - | `image/jpeg` |
| POST | `/servo/pan` | Set pan angle | `{"angle": float}` | `{"success": true}` |
| POST | `/servo/tilt` | Set tilt angle | `{"angle": float}` | `{"success": true}` |
| POST | `/servo/center` | Center camera | - | `{"success": true}` |

### Treat Dispenser

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| POST | `/treat/dispense` | Dispense one treat | - | `{"success": true, "remaining": int}` |
| POST | `/treat/carousel/rotate` | Rotate carousel | - | `{"success": true}` |

### LED Control

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| POST | `/led/pattern` | Set LED pattern | `{"pattern": string}` | `{"success": true}` |
| POST | `/led/color` | Set RGB color | `{"r": int, "g": int, "b": int}` | `{"success": true}` |
| POST | `/led/off` | Turn off LEDs | - | `{"success": true}` |

### Audio

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| POST | `/audio/play` | Play audio file | `{"file": string}` | `{"success": true}` |
| POST | `/audio/stop` | Stop playback | - | `{"success": true}` |
| POST | `/audio/volume` | Set volume | `{"level": int}` | `{"success": true}` |
| GET | `/audio/files` | List audio files | - | `{"files": string[]}` |

### Mode Control

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| GET | `/mode/get` | Get current mode | - | `{"mode": string}` |
| POST | `/mode/set` | Set mode | `{"mode": string}` | `{"success": true}` |

### Missions

| Method | Endpoint | Description | Request | Response |
|--------|----------|-------------|---------|----------|
| GET | `/missions` | List all missions | - | `Mission[]` |
| GET | `/missions/{id}` | Get mission details | - | `Mission` |
| POST | `/missions/{id}/start` | Start mission | - | `{"success": true}` |
| POST | `/missions/{id}/stop` | Stop mission | - | `{"success": true}` |
| GET | `/missions/active` | Get active mission | - | `Mission` or `null` |

---

## WebSocket Events

*(Commands section unchanged, adding WebRTC events)*

### WebRTC Events (NEW)

| Type | Direction | Description |
|------|-----------|-------------|
| `webrtc_request` | App → Relay | Request video stream |
| `webrtc_credentials` | Relay → App | TURN credentials |
| `webrtc_request` | Relay → Robot | Initiate stream with credentials |
| `webrtc_offer` | Robot → App | SDP offer |
| `webrtc_answer` | App → Robot | SDP answer |
| `webrtc_ice` | Bidirectional | ICE candidates |
| `webrtc_close` | Either → Either | Close stream |

### Existing Events (Robot → App)

| Event | Description |
|-------|-------------|
| `detection` | Dog detected/behavior change |
| `status` | Telemetry update |
| `treat` | Treat dispensed |
| `mission` | Mission progress |
| `error` | Error occurred |

### Existing Commands (App → Robot)

| Command | Description |
|---------|-------------|
| `motor` | Set motor speeds |
| `servo` | Set pan/tilt |
| `treat` | Dispense treat |
| `led` | Set LED pattern |
| `audio` | Play audio file |
| `mode` | Change mode |

---

## Implementation Checklist

### Robot (Raspberry Pi)
- [x] REST endpoints implemented
- [x] WebSocket server for events/commands
- [ ] **WebRTC service (aiortc)** ← NEW
- [ ] **Cloud relay client** ← NEW
- [ ] **Handle webrtc_request, create offer** ← NEW

### Mobile App (Flutter)
- [x] REST client implemented
- [x] WebSocket client implemented
- [ ] **WebRTC service (flutter_webrtc)** ← NEW
- [ ] **Request TURN credentials** ← NEW
- [ ] **Handle offer, send answer** ← NEW
- [ ] **Display WebRTC video** ← NEW

### Cloud Relay (FastAPI)
- [x] Device WebSocket handler
- [x] App WebSocket handler
- [x] Message routing
- [x] JWT authentication
- [ ] **Cloudflare TURN credential generation** ← NEW
- [ ] **WebRTC signaling message routing** ← NEW
- [ ] **POST /api/turn/credentials endpoint** ← NEW

### Cloudflare Setup
- [ ] Create Cloudflare account
- [ ] Navigate to Calls section in dashboard
- [ ] Create TURN App
- [ ] Save TURN_KEY_ID and TURN_KEY_API_TOKEN
- [ ] Add to relay server .env

---

## Cloudflare Calls Pricing

| Tier | Cost | Notes |
|------|------|-------|
| First 1 TB/month | Free | Generous for development/beta |
| Additional | $0.05/GB | ~$0.075/hour at 720p/15fps |

**Estimated costs:**
- 1 hour demo session: ~$0.08
- 100 beta users, 1 hour/day each: ~$240/month
- Production at scale: Negotiate volume pricing

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-01-18 | Initial contract |
| 1.1 | 2025-01-18 | Added WebRTC signaling, Cloudflare TURN integration |
