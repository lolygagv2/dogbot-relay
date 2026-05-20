# WIM-Z Cloud Relay Server - Product Roadmap
*Last Updated: 2026-05-20 (Build 50)*

## Overview

The WIM-Z Cloud Relay Server is the cloud infrastructure component that connects WIM-Z robots to mobile apps. It handles WebSocket message routing, WebRTC signaling, REST APIs for accounts/devices/dogs/media, and persistent storage.

## Architecture Position

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
                         │  Cloud Relay    │  ← THIS PROJECT
                         │  (FastAPI)      │
                         └────────┬────────┘
                                  │
                  ┌───────────────┼────────────────┐
                  │               │                │
         ┌────────▼──────┐ ┌──────▼──────┐ ┌───────▼────────┐
         │ Cloudflare    │ │ SQLite DB   │ │ Media storage  │
         │ TURN          │ │ (relay.db)  │ │ (uploads dir)  │
         └───────────────┘ └─────────────┘ └────────────────┘
```

## Current Status

### Phase 1: Core Infrastructure ✅ COMPLETE
- [x] FastAPI application structure
- [x] Pydantic configuration from environment
- [x] Health check + stats endpoints
- [x] CORS middleware

### Phase 2: Authentication ✅ COMPLETE
- [x] JWT token generation and validation
- [x] Per-device HMAC signature verification (timestamped)
- [x] Auth router (register / login / me)

### Phase 3: WebSocket Management ✅ COMPLETE
- [x] ConnectionManager for tracking connections
- [x] Robot (`/ws/device`) and app (`/ws/app`) endpoints + generic `/ws`
- [x] Message routing between app and robot
- [x] Device ownership tracking (database-backed)
- [x] Single-session enforcement, stale command rejection (>2s dropped)
- [x] `user_connected` / `user_disconnected` lifecycle events

### Phase 4: WebRTC Signaling ✅ COMPLETE
- [x] Cloudflare TURN service integration (24h TTL)
- [x] TURN credentials endpoint (POST + GET)

### Phase 5: WebRTC Signaling Implementation ✅ COMPLETE
- [x] `webrtc_request` handling — session ID, TURN creds, forward to robot
- [x] `webrtc_offer` / `webrtc_answer` / `webrtc_ice` routing
- [x] `webrtc_close` cleanup
- [x] Active WebRTC session tracking, single session per device

### Phase 6: Production Hardening 🔄 PARTIAL
- [x] Command rate limiting (ghost-command hardening)
- [x] Client IP logging throughout
- [x] Diagnostic logging (set_mode, status_update mode field, schedule/mission)
- [x] Connection stability / disconnect grace period
- [x] Drive-command fast path + uvloop (latency reduction)
- [ ] Error tracking (Sentry or similar)
- [ ] Metrics/monitoring export (Prometheus)
- [ ] Formal graceful shutdown audit

### Phase 7: AWS Lightsail Deployment ✅ COMPLETE (live)
- [x] Lightsail instance running, served at `api.wimzai.com`
- [x] Environment variables in `.env`
- [x] Per-device HMAC secrets for registered robots (03–05)
- Note: ongoing ops happen directly on the server.

### Phase 8: Persistence ✅ COMPLETE (SQLite)
- [x] SQLite database (`app/database.py`) — users, dogs, user_dogs, robots,
      dog_photos, dog_metrics, mission_log, mission_schedules, user_settings,
      activity_events, voice_commands, device_events
- [ ] (Future) migrate to PostgreSQL if scale requires it

### Build 48–50 Feature Work ✅ COMPLETE
- [x] Build 48: treat_counter_set logging, dog profile sync to robot
- [x] Build 49: media upload/download endpoints for robot video delivery
- [x] Build 50 Phase 1: `session_hello` handshake + dog profile schema
- [x] Build 50 Phase 2: voice command sync
- [x] Build 50 Phase 3: activity event log

---

## Remaining / Future Work

- Phase 6 hardening leftovers: error tracking, metrics export, shutdown audit
- Rate-limit tuning under real load
- PostgreSQL migration (only if SQLite becomes a bottleneck)
- Redis for multi-instance session state (only if horizontally scaling)
- Deprecate the legacy `/missions/schedule/*` route aliases once the app
  fully moves to `/schedules/*`

---

## API Surface (Build 50)

| Area | Prefix | Notes |
|------|--------|-------|
| Health/Debug | `/health`, `/stats`, `/debug/pairing`, `/debug/latency` | |
| Auth | `/api/auth/*` | register, login, me |
| Device | `/api/device/*` | register, pair, list, get, delete |
| User | `/api/user/*` | pair/unpair device, devices, delete |
| Dogs | `/api/dogs/*` | CRUD + photos |
| Metrics | `/api/metrics/*` | log, get, history |
| Events | `/api/events/{device_id}` | dashboard, summary |
| Activity | `/api/activity` | activity event log |
| Voice Commands | `/api/voice-commands/*` | CRUD + file |
| Media | `/api/media/*` | upload, download |
| Music | `/api/music/*` | upload, file get/delete |
| Schedules | `/schedules/*`, `/missions/schedule/*` | legacy aliases, deprecated tag |
| TURN | `/api/turn/credentials` | POST + GET |
| WebSocket | `/ws/device`, `/ws/app`, `/ws` | |

---

## Success Metrics

**Operational KPIs:**
- WebSocket connection success rate: >99%
- Message routing latency: <50ms
- TURN credential generation: <200ms
- Uptime: 99.9%

**Scale Targets:**
- Initial: 100 concurrent robots
- Phase 1: 1,000 concurrent robots
- Phase 2: 10,000 concurrent robots

---

*This roadmap reflects the relay server component only. See wimzapp and dogbot repos for mobile app and robot roadmaps.*
