# WIM-Z Cloud Relay Server - Project Directory Structure
*Last Updated: 2026-01-20 - FastAPI Relay Server*

## Overview

This is the **WIM-Z Cloud Relay Server** - a FastAPI application that connects WIM-Z robots to mobile apps via WebSocket. It handles message routing and WebRTC signaling but does NOT process video.

**Architecture:**
```
Mobile App ←→ Cloud Relay Server ←→ WIM-Z Robot
     (this repo)
```

## Project Structure

```
/home/morgan/wimzrelay/   # WIM-Z Cloud Relay Server

   .claude/                    # Claude AI session management
      CLAUDE.md                   # Development rules (DO NOT DELETE)
      DEVELOPMENT_PROTOCOL.md     # Development workflow rules
      WIM-Z_Project_Directory_Structure.md  # THIS FILE
      product_roadmap.md          # Relay server development phases
      development_todos.md        # Priority tasks
      commands/                   # Session commands
          session_start.md        # Session initialization
          session_end.md          # Session cleanup

   app/                         # FastAPI application package
      __init__.py                 # Package init
      main.py                     # FastAPI app entry point
      config.py                   # Settings from environment (pydantic-settings)
      models.py                   # Pydantic request/response models
      auth.py                     # JWT auth + device signature verification
      connection_manager.py       # WebSocket connection tracking & routing

      routers/                  # API route handlers
         __init__.py
         auth.py                  # POST /api/auth/* - Login, token refresh
         device.py                # Device registration/management
         turn.py                  # POST /api/turn/credentials - TURN creds
         websocket.py             # WS /ws/app, /ws/device - WebSocket endpoints

      services/                 # Business logic services
         __init__.py
         turn_service.py          # Cloudflare TURN API integration

   # Config files (root)
   run.py                       # Development server runner
   requirements.txt             # Python dependencies
   .env.example                 # Environment variable template
   .gitignore                   # Git ignore patterns
   API_CONTRACT_v1.1.md         # API specification (shared with robot/app)
   CLAUDE_INSTRUCTIONS_RELAY.md # WebRTC implementation guide
```

## Key Files by Function

### **Application Entry**
- `app/main.py` - FastAPI app creation, middleware, router includes
- `run.py` - Development server startup script

### **Configuration**
- `app/config.py` - Pydantic Settings class, loads from environment
- `.env.example` - Template for required environment variables

### **Authentication**
- `app/auth.py` - JWT token creation/validation, device signature verification
- `app/routers/auth.py` - Login and token refresh endpoints

### **WebSocket Management**
- `app/connection_manager.py` - Tracks all WebSocket connections, routes messages
- `app/routers/websocket.py` - WebSocket endpoints for apps and devices

### **WebRTC Support**
- `app/services/turn_service.py` - Generates Cloudflare TURN credentials
- `app/routers/turn.py` - TURN credential endpoint

### **API Specification**
- `API_CONTRACT_v1.1.md` - Complete API contract shared with robot and app
- `CLAUDE_INSTRUCTIONS_RELAY.md` - WebRTC implementation details

## Dependencies

From `requirements.txt`:
- `fastapi` - Web framework
- `uvicorn` - ASGI server
- `websockets` - WebSocket support
- `python-jose` - JWT token handling
- `passlib` - Password hashing
- `pydantic` / `pydantic-settings` - Data validation
- `httpx` - Async HTTP client (for Cloudflare API)

## Environment Variables

Required in `.env`:
```bash
# Server
HOST=0.0.0.0
PORT=8000
DEBUG=true

# Security
JWT_SECRET=<random-secret>
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

# Cloudflare TURN
CLOUDFLARE_TURN_KEY_ID=<from-cloudflare>
CLOUDFLARE_TURN_API_TOKEN=<from-cloudflare>
```

## How Claude Finds Files

When answering questions about relay server functionality:

1. **For "API endpoints"** → Check `app/routers/*.py`
2. **For "authentication"** → Check `app/auth.py` and `app/routers/auth.py`
3. **For "WebSocket handling"** → Check `app/connection_manager.py` and `app/routers/websocket.py`
4. **For "configuration"** → Check `app/config.py`
5. **For "data models"** → Check `app/models.py`
6. **For "WebRTC/TURN"** → Check `app/services/turn_service.py` and `app/routers/turn.py`
7. **For "API specification"** → Check `API_CONTRACT_v1.1.md`

---

*This structure document is the authoritative reference for file locations in the WIM-Z Cloud Relay Server.*
