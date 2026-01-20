# WIM-Z Relay Server - Session Start Command

## Initialization Protocol

### 1. Load Project Context
- Read `.claude/CLAUDE.md` for development rules
- Read `.claude/DEVELOPMENT_PROTOCOL.md` for workflow protocol
- Read `.claude/WIM-Z_Project_Directory_Structure.md` for file locations
- Review `.claude/product_roadmap.md` for current phase
- Check `.claude/development_todos.md` for active tasks

### 2. System Status Check

**Git Status:**
```bash
git status --short
git branch --show-current
git log --oneline -3
```

**Server Status (if running):**
```bash
curl -s http://localhost:8000/health 2>/dev/null || echo "Server not running"
curl -s http://localhost:8000/stats 2>/dev/null || echo "Stats unavailable"
```

### 3. Environment Check
```bash
# Check Python environment
python3 --version
pip list | grep -E "fastapi|uvicorn|pydantic"

# Check if .env exists
ls -la .env 2>/dev/null || echo "No .env file - copy from .env.example"
```

### 4. Ask User for Session Goal
Present options:
```
What are we working on today?

A. WebSocket Development (connection handling, message routing)
B. Authentication (JWT, device signatures)
C. WebRTC Signaling (TURN credentials, offer/answer routing)
D. API Endpoints (new routes, request/response models)
E. AWS Deployment (Docker, ECS, load balancer)
F. Testing & Debugging
G. General Development (specify task)

Enter letter or describe custom task:
```

### 5. Final Confirmation
```
Session initialized for: [USER GOAL]
Git status: [CLEAN / X uncommitted files]
Current branch: [BRANCH NAME]

Ready to begin. Proceed? (yes/no)
```

---

## Usage
Call this command at the start of every Claude Code session:
```bash
/project:session-start
```

## CRITICAL RULES
- NEVER skip project context loading
- NEVER assume session goal - always ask
- NEVER make changes before user confirmation
- ALWAYS show git status first
