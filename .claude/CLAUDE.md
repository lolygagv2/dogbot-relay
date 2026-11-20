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
| POST /events | Robot (HMAC) typed event ingest → `events` table (Dec 9 build) |
| GET /summary/{dog}/{date} | Daily roll-up per dog (today / yesterday / YYYY-MM-DD) |
| GET /api/dogs/{dog}/events, /status | Raw events; latest robot status + treat/quiet-hours context |
| GET/PUT /api/dogs/{dog}/settings/{key} | Shared settings with guards (app, connector, agent; robot reads all, writes only `treat_schedule`). Treat cap/hourly limit: changeable by every actor (0-100 / 0-60); an AI raising one pushes the owner an alert; the app's Settings treat limit (`mission_config` `data.daily_limit` / `daily_limit_enabled`; off = unlimited via `daily_cap_enabled: false`) is mirrored, debounced 1 s, into `treat_schedule`. `treat_schedule` is account-wide: a change on one dog is copied to all the owner's dogs |
| POST /api/dogs/{dog}/command | Allowlisted robot control (+ `wait_s` for command_result). `smart_plug` (diffuser plug on the robot's LAN): app any action, assistants on/off/status only; robot `smart_plug` events → own activity type, no push, and copied into `events` (one row per dog, id `<event_id>:<dog_id>`) so `get_events` / day summary see them; `ultrasonic` robot events the same (2026-11-13). `ultrasonic` (tweeter): relay checks style / custom burst (20/25/32/40 kHz, ≤80 %, ≤5 s) before the robot's own 20 s cooldown; assistants via `play_ultrasonic` (opt-in `ultrasonic` scope, only when the owner asks). `look` (camera pan/tilt step, 5-45°, physical scope). `set_sleep {asleep}` (ROBOT-STATE v2, 2026-11-19; sleep is NOT a mode, `sleep`/`wake` commands and `set_mode sleep` are gone): EVERY command (but set_sleep, select_dog, download_song, and the app's reopen frames sg_config, reload_dogs, smart_plug status (2026-11-20): the robot gets those asleep, no wake, no greeting) to a robot whose state says asleep sends `set_sleep false` first, waits ≤6 s for it, then goes out (app WS: held in the background; drive frames start the wake and drop; not confirmed → robot_timeout and the command is never sent). Every other command is answered within 2 s (callers' longer waits for listen / photo / move / trick / plug kept); robot `error: "expired"` (could not begin in time) is passed through, connector tools get `{ok:false, refused:true, reason:"expired"}`; late results still update the state; only an explicit wake (app / connector `set_sleep false` with success; never wake-first, select_dog, download_song) fires the wake greeting (`robot_woke`): recorded ahead while asleep (`greeting_prepare`), play_tts ≤1000 ms after the result, skipped within 60 s of a power-on greeting (and vice versa); it names only `selected_dog_id`; connector `set_sleep` (settings scope). Missions: `trigger.date` = one-time (robot deletes it after the run, `schedule_deleted {once}`); trigger kinds `schedule` / `manual` only. No departure/arrival triggers anywhere (2026-11-18): `music_plan` = `{enabled, volume, on_mode_start {mode, delay_s}, after_treat {every_n, minutes = rolling window}, on_bark_episode {min_severity}, at_time [{time}]}`, each with `track` + `duration_min`, run by the robot, set via `set_music`; roll-up has no `alone_s`, `sg_s` = time on guard |
| GET /api/devices/{device}/state, POST …/selected_dog, POST …/command (agent play_tts only, no dog) | ROBOT-STATE v2 (2026-11-19, `app/services/robot_state.py`): relay-owned state per robot (robot_online, mode idle/silent_guardian/coach/mission/manual, asleep, selected_dog_id(+_applied), session_dog_id, state_version, source, reason), persisted in `robot_state`; pushed to apps as `robot_state` frames on every change and on connect. set_mode / set_sleep / select_dog wait ≤2 s for command_result (`success`, never `ok`); offline → robot_offline, timeout → robot_timeout; nothing queued except the one stored selection. Reconnect: the robot's first status reports `selected_dog_id` + `selected_dog_changed_locally`; flag true and the owner's dog (or null) → adopted (reason local_selection) and echoed back; otherwise a differing report gets the stored value, and a matching one confirms it (applied true, nothing sent); a robot that reports nothing gets an unapplied stored selection after 3 s. `mode_change` is stored with dog_id null; status `mode` is always the real robot, replay only under `replay`. The relay stamps `data.source` on every command; a caller's own source goes to `data.reason`. Connector / voice `select_dog` (settings scope); `set_silent_guardian`, `set_coach`, `schedule_mission` pass `dog_id` as `data.dog_id` (missions: `mission.data.dog_id`). Voice agent names only `selected_dog_id` and never looks up another dog (no "oldest dog"); with none it speaks via the dog-less device route |
| /api/dogs/{dog}/missions/drafts | Assistant-proposed missions, owner approves in app |
| /api/dogs/{dog}/ai/* | Connector list / revoke / audit for the app |
| /admin/replay/* | Demo replayer (admin JWT) |
| :8001 /mcp + OAuth routes | MCP server process (`run_mcp.py`, app/mcp_server/) |
| :8002 /agent/* | Voice agent process (`run_agent.py`, app/voice_agent/) |

Dec 9 demo build (2026-11-06): three processes share `data/wimz.db`; internal callers use
`INTERNAL_API_TOKEN` + `X-Actor` + `X-On-Behalf-Of`; see `deploy/README.md`, the briefs
`.claude/ROBOT_BRIEF_FROM_RELAY_2026-11-06.md` / `APP_BRIEF_FROM_RELAY_2026-11-06.md`, and
`tests/` (`venv/bin/pytest tests/`).

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
