# WIM-Z Cloud Relay Server - Product Roadmap
*Last Updated: January 2026*

## Overview

The WIM-Z Cloud Relay Server is the cloud infrastructure component that connects WIM-Z robots to mobile apps. It handles WebSocket message routing and WebRTC signaling for video streaming.

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
                         ┌────────▼────────┐
                         │ Cloudflare TURN │
                         │ (Credential Gen)│
                         └─────────────────┘
```

## Current Status

### Phase 1: Core Infrastructure ✅ COMPLETE
- [x] FastAPI application structure
- [x] Pydantic configuration from environment
- [x] Health check endpoint
- [x] CORS middleware

### Phase 2: Authentication ✅ COMPLETE
- [x] JWT token generation and validation
- [x] Device signature verification (HMAC)
- [x] Auth router with login endpoint

### Phase 3: WebSocket Management ✅ COMPLETE
- [x] ConnectionManager for tracking connections
- [x] Robot WebSocket endpoint (/ws/device)
- [x] App WebSocket endpoint (/ws/app)
- [x] Message routing between app and robot
- [x] Device ownership tracking

### Phase 4: WebRTC Signaling ✅ COMPLETE
- [x] Cloudflare TURN service integration
- [x] TURN credentials endpoint
- [x] WebRTC signaling message types defined
- [x] Ready for signaling message routing

---

## Remaining Work

### Phase 5: WebRTC Signaling Implementation 🔄 IN PROGRESS
- [ ] Handle `webrtc_request` from app
- [ ] Forward `webrtc_offer` from robot to app
- [ ] Forward `webrtc_answer` from app to robot
- [ ] Route `webrtc_ice` candidates bidirectionally
- [ ] Handle `webrtc_close` cleanup
- [ ] Track active WebRTC sessions

### Phase 6: Production Hardening
- [ ] Rate limiting
- [ ] Request logging and monitoring
- [ ] Error tracking (Sentry or similar)
- [ ] Connection timeout handling
- [ ] Graceful shutdown

### Phase 7: AWS Lightsail Deployment
- [ ] Set up Lightsail instance (Ubuntu)
- [ ] Configure security group (ports 8000, 22)
- [ ] Install Python and dependencies
- [ ] Set up environment variables (.env)
- [ ] Configure domain and SSL (Let's Encrypt)
- [ ] Set up process manager (systemd or supervisor)

### Phase 8: Database Integration (Future)
- [ ] PostgreSQL for persistent data
- [ ] User account storage
- [ ] Device registration storage
- [ ] Session history logging

---

## API Endpoints Summary

| Endpoint | Method | Status | Purpose |
|----------|--------|--------|---------|
| `/health` | GET | ✅ | Health check |
| `/stats` | GET | ✅ | Connection statistics |
| `/api/auth/login` | POST | ✅ | Get JWT token |
| `/api/turn/credentials` | POST | ✅ | Generate TURN creds |
| `/ws/app` | WS | ✅ | App WebSocket |
| `/ws/device` | WS | ✅ | Robot WebSocket |

---

## Dependencies

**Core:**
- FastAPI + Uvicorn
- Pydantic + pydantic-settings
- python-jose (JWT)
- websockets

**External Services:**
- Cloudflare Calls (TURN service)

**Future:**
- PostgreSQL (user/device storage)
- Redis (session caching, optional)

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
