# WIM-Z API Contract v1.3

> **Single source of truth** for Robot, Mobile App, and Cloud Relay Server.
> When changing any API, update this file FIRST, then update each project to match.

Last Updated: 2026-02-22
Version: 1.3 - Always-on audio streaming, mode/UX restructure, resolution constraints

---

## Architecture Overview

```
┌─────────────────┐                                    ┌─────────────────┐
│   Mobile App    │◄──── WebRTC Video+Audio (P2P/TURN)►│   WIM-Z Robot   │
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
- **Video + Audio**: App ↔ Robot direct via WebRTC (high bandwidth, P2P when possible)
- **Push-to-Talk Audio**: App → Relay → Robot (WebSocket, base64 encoded, app→robot only)
- **TURN Relay**: Cloudflare handles NAT traversal when P2P fails

---

## Mode System

### Mode Definitions

| Mode | Resolution | AI Active | Audio Stream | Orientation | Entry Point |
|------|-----------|-----------|-------------|-------------|-------------|
| `idle` | Standard | No | Mic ON | Portrait | Default / exit from any mode |
| `manual` | High (up to 4K) | No | Mic ON | Landscape | Drive button |
| `silent_guardian` | 640x640 | Yes | Mic OFF | Portrait | Portrait dropdown |
| `coach` | 640x640 | Yes | Mic OFF | Portrait or Landscape | Portrait dropdown or Landscape selector |
| `mission` | 640x640 | Yes | Mic OFF | Landscape | Mission flow → Start Mission |

### Mode Resolution Constraint (NEW in v1.3)

**Hard rule**: Manual mode = high resolution, no AI processing. All AI modes (silent_guardian, coach, mission) = 640x640 with Hailo AI active. These are mutually exclusive — you cannot run AI detection at high resolution.

### Mode Transition Rules (NEW in v1.3)

**Portrait dropdown options**: idle, silent_guardian, coach (3 options only)

**Landscape selector options**: manual, coach, mission (mission only if a mission is loaded)

**Transition behaviors:**
- Entering Drive (landscape) from portrait: App stores current portrait mode as `previous_portrait_mode`. Robot switches to `manual`.
- Exiting Drive (landscape) back to portrait: App restores `previous_portrait_mode` (idle, silent_guardian, or coach). Robot receives `set_mode` with the restored mode.
- Selecting coach/silent_guardian from landscape: App exits landscape, returns to portrait, activates selected mode.
- Exiting mission (complete, failed, or cancelled): App returns to portrait. Restores `previous_portrait_mode`.
- Coach in landscape: Stays in landscape. Robot runs coach AI at 640x640 while user has manual drive controls.

**set_mode command now includes source context:**
```json
{
  "type": "command",
  "command": "set_mode",
  "data": {
    "mode": "manual",
    "source": "drive_enter",
    "timestamp": "ISO8601"
  }
}
```

Source values: `dropdown`, `drive_enter`, `drive_exit_restore`, `mission_start`, `mission_end`, `landscape_selector`

---

## WebRTC Audio Streaming (NEW in v1.3)

### Always-On Robot Microphone

The robot includes a **mono audio track** in the WebRTC PeerConnection alongside the video track. This audio track is always negotiated in the SDP offer/answer — it is never added or removed dynamically.

**Robot-side behavior:**
- In `idle` and `manual` modes: USB mic feeds audio frames to the WebRTC audio track.
- In `silent_guardian`, `coach`, and `mission` modes: Audio track remains in SDP but robot sends silence (mic muted server-side on Pi). No renegotiation needed.
- On mode change: Robot mutes/unmutes the audio track source. No WebRTC renegotiation.

**App-side behavior:**
- Audio playback is controlled entirely client-side via a mute/unmute toggle.
- Mute state persists locally across sessions (SharedPreferences / UserDefaults).
- Default on first use: **muted**.
- The mute toggle does NOT send any command to the robot. It only controls local audio playback.
- When the robot's mic is server-side muted (AI modes), the app receives silence regardless of mute toggle state.

**Audio specifications (WebRTC track):**
- Codec: Opus (WebRTC default)
- Sample rate: 48kHz mono (Opus default, downsampled from 16kHz mic input)
- Bitrate: 32-64 kbps (adaptive)
- Latency: ~100-300ms depending on network

**Adaptive quality:**
- Robot monitors WebRTC connection stats (packet loss, RTT).
- If bandwidth is constrained, audio bitrate is reduced first before touching video.
- If severe congestion, audio track can be temporarily muted server-side to preserve video.

### Push-to-Talk: App → Robot (UNCHANGED)

PTT remains on WebSocket via relay. This is intentional — PTT is intermittent, user-initiated, and does not require a persistent reverse audio track.

**App sends recorded audio:**
```json
{
  "type": "audio_message",
  "data": "<base64_encoded_audio>",
  "format": "aac",
  "duration_ms": 3500
}
```

**Robot plays through speaker and acknowledges:**
```json
{
  "type": "audio_played",
  "success": true
}
```

**Echo handling:** When the robot receives and plays a PTT message, it auto-mutes its outbound WebRTC audio track for the duration of playback to prevent echo/feedback. It resumes feeding the audio track after playback completes.

**PTT Audio Specifications (WebSocket):**
- Format: AAC (preferred) or WAV
- Sample rate: 16kHz mono
- Max duration: 10 seconds
- Max size: ~100KB for 10s AAC

### DEPRECATED: audio_request / audio_message (Robot → App)

The `audio_request` command (app requesting robot mic capture) and the robot→app `audio_message` response are **deprecated** as of v1.3. The always-on WebRTC audio track replaces this functionality. These message types will be removed in v1.4.

---

## WebSocket Commands (App → Robot)

### Core Commands

| Command | Data | Description |
|---------|------|-------------|
| `motor` | `{"left": float, "right": float}` | Set motor speeds (-1.0 to 1.0) |
| `servo` | `{"pan": float, "tilt": float}` | Set camera pan/tilt angles |
| `servo_center` | `{}` | Center camera to default position |
| `dispense_treat` | `{}` | Dispense one treat |
| `led` | `{"pattern": string}` | Set LED pattern |
| `audio` | `{"file": string}` | Play audio file |
| `audio_toggle` | `{}` | Play/pause current audio |
| `audio_volume` | `{"volume": int}` | Set volume (0-100) |
| `audio_next` | `{}` | Skip to next track |
| `audio_prev` | `{}` | Go to previous track |
| `set_mode` | `{"mode": string, "source": string, "timestamp": string}` | Change robot mode (v1.3: added source and timestamp) |

### Photo Capture

| Command | Data | Description |
|---------|------|-------------|
| `take_photo` | `{"with_hud": bool}` | Capture photo, optionally with AI overlay |

**Request:**
```json
{
  "type": "command",
  "command": "take_photo",
  "data": {"with_hud": true}
}
```

**Robot Response (sent back via WebSocket):**
```json
{
  "type": "photo",
  "data": "<base64_encoded_jpeg>",
  "timestamp": "2026-01-26T02:15:00Z",
  "filename": "wimz_20260126_021500.jpg",
  "with_hud": true
}
```

**HUD Overlay includes:**
- Dog bounding box (if detected)
- Dog name label
- Current mode indicator
- Timestamp

### Call Dog

| Command | Data | Description |
|---------|------|-------------|
| `call_dog` | `{"dog_id": string?}` | Play dog's name recording or default call |

**Request:**
```json
{
  "type": "command",
  "command": "call_dog",
  "data": {"dog_id": "dog_001"}
}
```

**Robot behavior:**
1. If custom voice recording exists for dog → play `dog_001.mp3`
2. If no custom recording → play generic `come_here.mp3`
3. LED pattern: attention (blue pulse)

---

## WebSocket Events (Robot → App)

### Status Events

| Event | Description |
|-------|-------------|
| `battery` | Battery level update |
| `status_update` | Mode change or system status |
| `alert` | Safety or system alert |
| `detection` | Dog detected/behavior change |
| `treat` | Treat dispensed confirmation |
| `mission` | Mission progress update |
| `error` | Error occurred |

### Photo Event

```json
{
  "type": "photo",
  "data": "<base64_jpeg>",
  "timestamp": "ISO8601",
  "filename": "wimz_YYYYMMDD_HHMMSS.jpg",
  "with_hud": true
}
```

### Audio Played Event

```json
{
  "type": "audio_played",
  "success": true
}
```

---

## LED Patterns

| Pattern | Description | Colors |
|---------|-------------|--------|
| `rainbow` | Cycling rainbow | Multi |
| `pulse` | Breathing effect | Current color |
| `solid` | Solid color | Current color |
| `chase` | Chasing lights | Current color |
| `fire` | Fire flicker effect | Orange/red |
| `warning` | Alert flash | Orange/red fast flash |
| `attention` | Dog attention getter | Blue pulse |
| `celebration` | Treat celebration | Multi sparkle |
| `off` | LEDs off | None |

---

## REST API Endpoints (Robot Local)

### Health & Status

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Server alive check |
| GET | `/telemetry` | Full system status |

### Motor Control

| Method | Endpoint | Request | Description |
|--------|----------|---------|-------------|
| POST | `/motor/speed` | `{"left": float, "right": float}` | Set motor speeds |
| POST | `/motor/stop` | - | Stop all motors |
| POST | `/motor/emergency` | - | Emergency stop |

### Camera & Servos

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/camera/stream` | MJPEG video stream (local) |
| GET | `/camera/snapshot` | Single JPEG frame |
| POST | `/servo/pan` | Set pan angle |
| POST | `/servo/tilt` | Set tilt angle |
| POST | `/servo/center` | Center camera |

### Treat Dispenser

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/treat/dispense` | Dispense one treat |
| POST | `/treat/carousel/rotate` | Rotate carousel |

### LED Control

| Method | Endpoint | Request | Description |
|--------|----------|---------|-------------|
| POST | `/led/pattern` | `{"pattern": string}` | Set LED pattern |
| POST | `/led/color` | `{"r": int, "g": int, "b": int}` | Set RGB color |
| POST | `/led/off` | - | Turn off LEDs |

### Audio

| Method | Endpoint | Request | Description |
|--------|----------|---------|-------------|
| POST | `/audio/play` | `{"file": string}` | Play audio file |
| POST | `/audio/stop` | - | Stop playback |
| POST | `/audio/volume` | `{"level": int}` | Set volume (0-100) |
| POST | `/audio/toggle` | - | Play/pause toggle |
| POST | `/audio/next` | - | Next track |
| POST | `/audio/prev` | - | Previous track |
| GET | `/audio/files` | - | List audio files |
| GET | `/audio/current` | - | Current track info |

### Mode Control

| Method | Endpoint | Request | Description |
|--------|----------|---------|-------------|
| GET | `/mode/get` | - | Get current mode |
| POST | `/mode/set` | `{"mode": string}` | Set mode |

---

## WebRTC Signaling

| Message Type | Direction | Description |
|--------------|-----------|-------------|
| `webrtc_request` | App → Relay | Request video+audio stream |
| `webrtc_credentials` | Relay → App | TURN credentials |
| `webrtc_request` | Relay → Robot | Initiate with credentials |
| `webrtc_offer` | Robot → App | SDP offer (includes video + audio tracks) |
| `webrtc_answer` | App → Robot | SDP answer |
| `webrtc_ice` | Bidirectional | ICE candidates |
| `webrtc_close` | Either | Close stream |

**v1.3 Change:** The SDP offer from the robot now always includes both a video track and an audio track. The app must accept both tracks in the SDP answer.

---

## Message Envelope Format

All WebSocket messages use this envelope:

**App → Relay → Robot (Command):**
```json
{
  "type": "command",
  "command": "<command_name>",
  "data": { ... },
  "device_id": "wimz_robot_01"
}
```

**Robot → Relay → App (Event):**
```json
{
  "type": "<event_type>",
  "device_id": "wimz_robot_01",
  "data": { ... },
  "timestamp": "ISO8601"
}
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-01-18 | Initial contract |
| 1.1 | 2025-01-18 | Added WebRTC signaling, Cloudflare TURN |
| 1.2 | 2026-01-26 | Added take_photo, audio_message, audio_request, call_dog commands; documented LED patterns and modes |
| 1.3 | 2026-02-22 | Always-on WebRTC audio track (robot→app), deprecated audio_request/audio_message for listening, mode/UX restructure with resolution constraints, mode transition rules with source context, adaptive audio quality |
