# WIM-Z Cloud Relay Server - Project Directory Structure
*Last Updated: 2026-05-20 (Build 50)*

## Overview

This is the **WIM-Z Cloud Relay Server** - a FastAPI application that connects
WIM-Z robots to mobile apps via WebSocket. It handles message routing, WebRTC
signaling, REST APIs, and persistence, but does NOT process video.

**Architecture:**
```
Mobile App ←→ Cloud Relay Server ←→ WIM-Z Robot
              (this repo, api.wimzai.com)
```

## Project Structure

```
/home/morgan/wimzrelay/   # WIM-Z Cloud Relay Server

   .claude/                    # Claude AI session management
      CLAUDE.md                   # Development rules (DO NOT DELETE)
      DEVELOPMENT_PROTOCOL.md     # Development workflow rules
      WIM-Z_Project_Directory_Structure.md  # THIS FILE
      product_roadmap.md          # Development phases
      development_todos.md        # Priority tasks
      API_CONTRACT_v1_3.md        # Current API contract (source of truth)
      commands/                   # Session commands (session_start/end)
      hooks/                      # Claude Code hooks
      skills/                     # Claude Code skills

   app/                         # FastAPI application package
      __init__.py
      main.py                     # FastAPI app entry, router includes, health/debug
      config.py                   # Settings from environment (pydantic-settings)
      models.py                   # Pydantic request/response models
      auth.py                     # JWT auth + per-device HMAC signature verification
      connection_manager.py       # WebSocket + WebRTC session tracking & routing
      database.py                 # SQLite schema + data access

      routers/                  # API route handlers
         __init__.py
         auth.py                  # /api/auth/*   - register, login, me
         device.py                # /api/device/* - register, pair, list, get, delete
         user.py                  # /api/user/*   - pair/unpair device, devices
         dogs.py                  # /api/dogs/*   - dog CRUD + photos
         metrics.py               # /api/metrics/* - dog metrics log/get/history
         events.py                # /api/events/* - dashboard events + summary
         activity.py              # /api/activity - activity event log
         voice_commands.py        # /api/voice-commands/* - voice command CRUD + file
         media.py                 # /api/media/*  - upload/download (robot video)
         music.py                 # /api/music/*  - music upload + file get/delete
         schedule.py              # /schedules/*, /missions/schedule/* (legacy aliases)
         turn.py                  # /api/turn/credentials - Cloudflare TURN creds
         websocket.py             # /ws/device, /ws/app, /ws + WebRTC signaling

      services/                 # Business logic services
         __init__.py
         turn_service.py          # Cloudflare TURN API integration

   archive/                     # Retired build notes and task assignments

   # Config / runtime (root)
   run.py                        # Development server runner
   requirements.txt              # Python dependencies
   .env / .env.example           # Environment variables (.env = secrets, DO NOT DELETE)
   .gitignore
   API_CONTRACT_v1.1.md          # Older API spec (superseded by .claude/API_CONTRACT_v1_3.md)
   CLAUDE_INSTRUCTIONS_RELAY.md  # WebRTC implementation guide
   MODE_AUDIT_FINDINGS.md        # Session lifecycle / stale command audit
```

## Key Files by Function

### Application Entry
- `app/main.py` - FastAPI app, middleware, router includes, `/health` `/stats` `/debug/*`
- `run.py` - Development server startup script

### Configuration
- `app/config.py` - Pydantic Settings, loads from environment
- `.env.example` - Template for required environment variables

### Authentication
- `app/auth.py` - JWT token creation/validation, per-device HMAC signatures
- `app/routers/auth.py` - register / login / me endpoints

### WebSocket & WebRTC
- `app/connection_manager.py` - Connection + WebRTC session tracking, routing
- `app/routers/websocket.py` - WS endpoints + WebRTC signaling handlers

### Persistence
- `app/database.py` - SQLite schema (users, dogs, robots, metrics, schedules,
  activity_events, voice_commands, device_events, etc.) and access functions

### API Specification
- `.claude/API_CONTRACT_v1_3.md` - Current API contract (source of truth)
- `CLAUDE_INSTRUCTIONS_RELAY.md` - WebRTC implementation details

## How Claude Finds Files

1. **API endpoints** → `app/routers/*.py` (see prefixes above)
2. **Authentication** → `app/auth.py`, `app/routers/auth.py`
3. **WebSocket / WebRTC** → `app/connection_manager.py`, `app/routers/websocket.py`
4. **Configuration** → `app/config.py`
5. **Data models** → `app/models.py`
6. **Database / persistence** → `app/database.py`
7. **TURN credentials** → `app/services/turn_service.py`, `app/routers/turn.py`
8. **API specification** → `.claude/API_CONTRACT_v1_3.md`

---

*This structure document is the authoritative reference for file locations in the WIM-Z Cloud Relay Server.*
