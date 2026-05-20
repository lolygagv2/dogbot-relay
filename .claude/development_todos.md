# WIM-Z Relay Server - Development TODO List
*Last Updated: 2026-05-20 (Build 50)*

## Current Status: Live in production (`api.wimzai.com`), Build 50

Core infrastructure, auth, WebSocket routing, WebRTC signaling, REST APIs,
and SQLite persistence are all complete and deployed. Ongoing work happens
directly on the Lightsail server.

---

## ✅ COMPLETED

- Core infrastructure (FastAPI, config, health/stats)
- Authentication (JWT + per-device HMAC signatures)
- WebSocket infrastructure (ConnectionManager, robot/app endpoints, routing,
  single-session enforcement, stale-command rejection, lifecycle events)
- WebRTC signaling — full request/offer/answer/ice/close routing + session tracking
- Cloudflare TURN integration (24h TTL)
- REST APIs: auth, device, user, dogs, metrics, events, activity,
  voice-commands, media, music, schedules
- SQLite persistence (13 tables, `app/database.py`)
- AWS Lightsail deployment (live at `api.wimzai.com`)
- Diagnostic logging + command rate limiting (ghost-command hardening)
- set_mode source/timestamp passthrough + logging (see MODE_AUDIT_FINDINGS.md)
- Builds 48–50: media delivery, session_hello handshake, voice command sync,
  activity event log

---

## 🔄 OPEN - Production Hardening (Phase 6 leftovers)

- [ ] Error tracking (Sentry or similar)
- [ ] Metrics/monitoring export (Prometheus or similar)
- [ ] Formal graceful-shutdown audit
- [ ] Rate-limit tuning under real load

---

## 🔮 FUTURE

- [ ] Deprecate legacy `/missions/schedule/*` aliases once app moves to `/schedules/*`
- [ ] PostgreSQL migration (only if SQLite becomes a bottleneck)
- [ ] Redis for multi-instance session state (only if horizontally scaling)

---

## Key Files Reference

### Core Application
- `app/main.py` - FastAPI app entry, router includes, health/stats/debug
- `app/config.py` - Settings
- `app/models.py` - Pydantic models
- `app/database.py` - SQLite schema + access

### Authentication
- `app/auth.py` - JWT + per-device HMAC signature verification

### WebSocket / WebRTC
- `app/connection_manager.py` - Connection + WebRTC session tracking
- `app/routers/websocket.py` - WS endpoints + WebRTC signaling handlers

### REST Routers
- `app/routers/` - auth, device, user, dogs, metrics, events, activity,
  voice_commands, media, music, schedule, turn

### Services
- `app/services/turn_service.py` - Cloudflare TURN credentials

---

*Reflects relay server development status as of Build 50, 2026-05-20.*
