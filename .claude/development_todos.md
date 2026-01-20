# WIM-Z Relay Server - Development TODO List
*Last Updated: January 20, 2026*

## Current Status: WebRTC Signaling Implementation

### ✅ COMPLETED - Core Infrastructure
- [x] FastAPI application setup
- [x] Configuration from environment
- [x] Health check endpoint
- [x] Connection statistics endpoint

### ✅ COMPLETED - Authentication
- [x] JWT token generation
- [x] JWT token validation
- [x] Device signature verification
- [x] Auth router

### ✅ COMPLETED - WebSocket Infrastructure
- [x] ConnectionManager class
- [x] Robot connection handling
- [x] App connection handling
- [x] Message routing
- [x] Device ownership tracking

### ✅ COMPLETED - TURN Service
- [x] Cloudflare TURN integration
- [x] Credentials endpoint

---

## 🔄 IN PROGRESS - WebRTC Signaling

### Priority 1: Complete WebRTC Message Routing
- [ ] Handle `webrtc_request` in WebSocket handler
  - Generate session ID
  - Get TURN credentials
  - Forward to robot with credentials
  - Send credentials to app
- [ ] Route `webrtc_offer` from robot to app
- [ ] Route `webrtc_answer` from app to robot
- [ ] Route `webrtc_ice` bidirectionally
- [ ] Handle `webrtc_close` with cleanup

### Priority 2: Session Tracking
- [ ] Track active WebRTC sessions
- [ ] Clean up sessions on disconnect
- [ ] Handle session timeouts

---

## 🎯 NEXT - AWS Lightsail Deployment

### Lightsail Instance Setup
- [ ] Create Lightsail instance (Ubuntu 22.04)
- [ ] Configure firewall rules (ports 22, 8000, 443)
- [ ] SSH into instance and update packages

### Application Deployment
- [ ] Clone/upload code to instance
- [ ] Install Python 3.11+ and pip
- [ ] Install requirements (`pip install -r requirements.txt`)
- [ ] Create `.env` file with production secrets
- [ ] Test server runs (`python run.py`)

### Production Configuration
- [ ] Set up systemd service for auto-restart
- [ ] Configure Nginx as reverse proxy (optional)
- [ ] Set up Let's Encrypt SSL certificate

### Domain & SSL
- [ ] Set up domain (api.wimz.io or similar)
- [ ] Point domain to Lightsail static IP
- [ ] Update CORS settings for production

---

## 🔮 FUTURE - Enhancements

### Production Hardening
- [ ] Rate limiting middleware
- [ ] Request logging
- [ ] Error tracking (Sentry)
- [ ] Metrics collection (Prometheus)

### Database Integration
- [ ] Add PostgreSQL
- [ ] User account table
- [ ] Device registration table
- [ ] Session history table

### Scaling
- [ ] Redis for session state (multi-instance)
- [ ] Connection state synchronization
- [ ] Auto-scaling configuration

---

## Key Files Reference

### Core Application
- `app/main.py` - FastAPI app entry
- `app/config.py` - Settings
- `app/models.py` - Pydantic models

### Authentication
- `app/auth.py` - JWT + device auth

### WebSocket
- `app/connection_manager.py` - Connection tracking
- `app/routers/websocket.py` - WS endpoints

### WebRTC
- `app/services/turn_service.py` - TURN credentials
- `app/routers/turn.py` - TURN endpoint

---

*This TODO list reflects relay server development status as of January 20, 2026*
