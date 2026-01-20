# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**WIM-Z Cloud Relay Server** - A FastAPI-based cloud relay server that connects WIM-Z robots to the mobile app. This server:
- Routes WebSocket messages between robots and mobile apps
- Generates Cloudflare TURN credentials for WebRTC video streaming
- Handles JWT authentication for secure connections
- Does NOT process video - video flows directly peer-to-peer or through Cloudflare TURN

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Or use the run script
python run.py

# Run with production settings
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Architecture

**Layered structure:**

- `app/` - Main application package
  - `main.py` - FastAPI app entry point with routes and middleware
  - `config.py` - Settings from environment variables (pydantic-settings)
  - `models.py` - Pydantic models for requests/responses
  - `auth.py` - JWT authentication and device signature verification
  - `connection_manager.py` - WebSocket connection tracking and message routing
  - `routers/` - API route modules
    - `auth.py` - Authentication endpoints (/api/auth/*)
    - `device.py` - Device registration and management
    - `turn.py` - Cloudflare TURN credential generation
    - `websocket.py` - WebSocket endpoints for robots and apps
  - `services/` - Business logic services
    - `turn_service.py` - Cloudflare TURN API integration

## Key Patterns

- **Singleton ConnectionManager** - Global instance tracks all WebSocket connections
- **JWT Authentication** - Mobile apps authenticate with JWT tokens
- **Device Signatures** - Robots authenticate with HMAC signatures
- **WebSocket Message Routing** - Commands from app to robot, events from robot to app

## WebSocket Flow

```
Mobile App ←→ /ws/app (JWT auth) ←→ Relay Server ←→ /ws/device (signature auth) ←→ Robot
```

**Message Types:**
- App → Robot: `motor`, `servo`, `treat`, `led`, `audio`, `mode`, `webrtc_*`
- Robot → App: `status`, `detection`, `treat`, `mission`, `error`, `webrtc_*`

## WebRTC Signaling

The relay routes WebRTC signaling messages but does NOT process video:
1. App requests video: `webrtc_request`
2. Relay generates TURN credentials from Cloudflare
3. Relay forwards `webrtc_offer`, `webrtc_answer`, `webrtc_ice` between parties
4. Video flows directly between app and robot via WebRTC

## Environment Variables

```bash
# Server Config
HOST=0.0.0.0
PORT=8000
DEBUG=true
APP_NAME="WIM-Z Relay Server"

# Security
JWT_SECRET=your_jwt_secret_here
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

# Cloudflare TURN (for WebRTC)
CLOUDFLARE_TURN_KEY_ID=your_turn_key_id
CLOUDFLARE_TURN_API_TOKEN=your_turn_api_token
```

## API Quick Reference

| Endpoint | Purpose |
|----------|---------|
| GET /health | Health check |
| GET /stats | Connection statistics |
| POST /api/auth/login | Get JWT token |
| POST /api/auth/refresh | Refresh JWT token |
| POST /api/turn/credentials | Generate TURN credentials |
| WS /ws/app | Mobile app WebSocket (JWT auth) |
| WS /ws/device | Robot WebSocket (signature auth) |

## Testing

```bash
# Test health endpoint
curl http://localhost:8000/health

# Test TURN credentials (requires JWT)
curl -X POST http://localhost:8000/api/turn/credentials \
  -H "Authorization: Bearer YOUR_JWT" \
  -H "Content-Type: application/json" \
  -d '{"ttl": 3600}'
```

## AWS Lightsail Deployment Notes

This server is designed for AWS Lightsail deployment:
- Single instance deployment on Ubuntu Lightsail
- Run with `python run.py` (set `DEBUG=false` in .env for production)
- Use systemd service for auto-restart on failure/reboot
- Environment variables stored in `.env` file
- Health check endpoint at `/health` for monitoring
- Consider Nginx reverse proxy for SSL termination

---

# WIM-Z Relay Server - Development Rules

## NEVER DELETE OR MODIFY
- `.env` files (contains secrets)
- Any file with "KEEPME" or "NOTES" in filename

## Project Structure - MUST MAINTAIN
Follow the structure defined in `.claude/WIM-Z_Project_Directory_Structure.md`

**FastAPI server structure:**
- `app/` - All application code
- `app/routers/` - API route handlers
- `app/services/` - Business logic services
- `app/models.py` - Pydantic models

## Project Documentation
- `.claude/product_roadmap.md` - Relay server development phases
- `.claude/development_todos.md` - Priority-sorted tasks
- `API_CONTRACT_v1.1.md` - API specification (shared with robot/app)
- `CLAUDE_INSTRUCTIONS_RELAY.md` - WebRTC implementation guide

## Development Workflow
1. Plan changes in Plan Mode (Shift+Tab twice)
2. Get approval before file operations
3. Commit incrementally
4. Ask before creating >5 new files

## When Refactoring
- Preserve existing working code structure
- Clean up as you go, but ASK first
