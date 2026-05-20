# Build 39 - Key Questions Answered (Relay Perspective)

---

## 1. What WebSocket command does the tracking checkbox send?

**RELAY ANSWER:** Unknown - relay doesn't have special handling for a "tracking" command.

The relay is a pass-through - it forwards ANY command to the robot without validation. Looking at `websocket.py:740-804`, commands are forwarded verbatim:

```python
if "command" in message:
    cmd_type = message.get("command")
    # ... forwards to robot as-is
```

**ACTION NEEDED:** App team needs to confirm what command the tracking checkbox sends. Common possibilities:
- `servo_tracking` / `enable_tracking`
- `camera_track` / `track_dog`
- A setting update command?

**TO DEBUG:** Add this to relay temporarily:
```python
logger.info(f"[CMD-DEBUG] Full payload: {json.dumps(message)}")
```

---

## 2. What exact fields does start_mission send?

**RELAY ANSWER:** Relay doesn't enforce fields - it forwards whatever the app sends.

The relay logs show `start_mission` was routed (line from test):
```
02:10:56 [ROUTE] App(user_000003) -> Robot(wimz_robot_01): start_mission
```

**RELAY DOES NOT KNOW** what fields are inside. It just forwards.

**TO DEBUG:** Need app team to confirm what they send. Expected format likely:
```json
{
  "command": "start_mission",
  "mission_type": "sit_training",
  "dog_id": "dog_123",
  "stages": 5
}
```

---

## 3. Why is app calling REST /missions (404) instead of WebSocket list_missions?

**RELAY ANSWER:** The relay has NO `/missions` REST endpoint.

From logs:
```
02:01:34 "GET /missions HTTP/1.1" 404 Not Found
```

**This is an APP BUG** - the app is making a REST call to an endpoint that doesn't exist.

**OPTIONS:**
1. **App fix:** Use WebSocket `get_missions` or `list_missions` command instead
2. **Relay fix:** Add a static `/missions` endpoint that returns available mission types

**If relay should add it:**
```python
@router.get("/api/missions")
async def get_missions():
    return {
        "missions": [
            {"id": "sit_training", "name": "Sit Training"},
            {"id": "down_training", "name": "Down Training"},
            {"id": "stay_training", "name": "Stay Training"}
        ]
    }
```

**QUESTION FOR APP:** Do you want REST or WebSocket for this?

---

## 4. Can relay log full message payloads for debugging?

**RELAY ANSWER:** Yes, we can add verbose logging.

Currently relay only logs message type. To add full payload logging:

```python
# Add to websocket.py around line 398 (robot messages) and 676 (app messages)
if settings.debug:
    logger.debug(f"[PAYLOAD] {connection_type}({identifier}): {json.dumps(message)[:500]}")
```

**CAUTION:** Full payload logging can:
- Generate huge log files
- Expose sensitive data
- Should only be enabled temporarily for debugging

**RECOMMENDATION:** Add a `VERBOSE_LOGGING=true` env var that enables this.

---

## 5. Field name mismatch: Robot sends action, current_stage, state but relay expects status, stage, mission_type

**THIS IS THE ROOT CAUSE OF MISSION_PROGRESS SHOWING NULL!**

**Relay expects (from `websocket.py:497-501`):**
```python
data.get('status')        # Robot sends: action or state?
data.get('stage')         # Robot sends: current_stage
data.get('total_stages')  # Robot sends: ???
data.get('mission_type')  # Robot sends: ???
```

**Robot apparently sends:**
```json
{
  "event": "mission_progress",
  "data": {
    "action": "...",
    "current_stage": 1,
    "state": "running"
  }
}
```

**FIX OPTIONS:**

### Option A: Fix on ROBOT (recommended)
Robot should send standardized field names:
```json
{
  "event": "mission_progress",
  "data": {
    "status": "running",
    "stage": 1,
    "total_stages": 5,
    "mission_type": "sit_training"
  }
}
```

### Option B: Fix on RELAY (adapter pattern)
Relay normalizes field names before logging/forwarding:
```python
if event_name == "mission_progress":
    data = message.get("data", {})
    # Normalize field names
    status = data.get('status') or data.get('state') or data.get('action')
    stage = data.get('stage') or data.get('current_stage')
    # ... etc
```

### Option C: Fix on APP
App handles both field name variants when receiving `mission_progress`.

**RECOMMENDATION:** Option A (Robot fix) is cleanest. Option B (Relay adapter) is fastest if robot can't change soon.

---

## Summary of Who Fixes What

| Issue | Owner | Priority |
|-------|-------|----------|
| Field name mismatch | **ROBOT** (or relay adapter) | P0 - THIS IS WHY MISSIONS FAIL |
| REST /missions 404 | **APP** or **RELAY** | P1 |
| Tracking command unknown | **APP** to document | P2 |
| Full payload logging | **RELAY** can add | P2 |
| start_mission fields | **APP** to document | P2 |

---

## Immediate Relay Action Available

If you want, I can add a **field name adapter** to the relay right now that normalizes robot mission_progress data to match what the relay logs (and app) expect. This would be a quick fix while robot updates their field names.

Say the word and I'll implement it.
