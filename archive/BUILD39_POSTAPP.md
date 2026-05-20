# Build 39 - Relay Server Analysis of Test Results

**Date:** Feb 1, 2026
**Test Window:** 21:01-21:22 local (02:01-02:22 UTC on relay)

---

## Executive Summary

From the relay server perspective, most message routing is working correctly. The issues are primarily on the **Robot** and **App** sides. However, there are a few relay-side findings.

---

## Issue Analysis

### 1. Mission Mode - STUCK ON INITIALIZING

**Symptom:** User clicks Start Mission, button doesn't change to Stop, stuck on "Initializing Mission 12"

**Relay Log Evidence:**
```
02:14:45 [ROUTE] App -> Robot: start_mission
02:14:45 [MISSION] Progress event from wimz_robot_01: status=None stage=None/None mission_type=None
```

**Root Cause:** Robot is sending `mission_progress` with ALL NULL VALUES:
- `status=None`
- `stage=None/None`
- `mission_type=None`

**Who Needs to Fix:** ROBOT
- Robot is not populating the mission_progress event properly
- Should send: `status="running"`, `stage="1/5"`, `mission_type="sit_training"`, etc.

**Relay Action:** None needed - relay is routing correctly. Could add validation/warning for empty mission data.

---

### 2. MP3 Upload - ERROR 413

**Symptom:** "Error 413" or "file too large for server" from "RobotAPI"

**Relay Log Evidence:** NO upload attempts in logs during test window!

**Root Cause:** The 413 is coming from the **ROBOT's web server**, NOT the relay!

Flow happening:
```
App → Relay (20MB limit, nginx 50M) → Robot API (UNKNOWN LIMIT) → 413 REJECTED
```

**Who Needs to Fix:**
1. **ROBOT** - Check if robot has its own HTTP server with size limits
2. **APP** - The error message says "RobotAPI" which confirms it's robot-side

**Relay Action:** Already fixed (20MB code, 50M nginx). Relay is not the blocker.

---

### 3. Missing /missions Endpoint

**Relay Log Evidence:**
```
02:01:34 INFO: 98.109.34.178:0 - "GET /missions HTTP/1.1" 404 Not Found
```

**Root Cause:** App is calling `GET /missions` but relay has no such endpoint.

**Who Needs to Fix:**
- **RELAY** - Add `/missions` endpoint if needed, OR
- **APP** - Remove this call if missions should come from robot via WebSocket

**Question for App Team:** What data do you expect from `GET /missions`?
- Static list of available mission types? (relay can provide)
- User's mission history? (need database)
- Robot's current mission state? (should use WebSocket)

---

### 4. Scheduler - "Failed to Create"

**Symptom:** App shows "failed to create schedule"

**Relay Log Evidence:**
```
02:19:35 [ROUTE] App -> Robot: create_schedule
02:19:35 [ROUTE] Robot -> App: schedule_created
02:19:35 [EVENT-OK] schedule_created delivered to 1 app(s)
```

**Root Cause:** Schedule WAS created successfully! Robot confirmed it, relay delivered it.

**Who Needs to Fix:** APP
- App is showing error despite receiving `schedule_created` event
- Check app's WebSocket handler for `schedule_created`

---

### 5. Coach Mode - Voice/AI Inconsistencies

**Symptom:** Sometimes says trick name, sometimes doesn't. No "sit 34%" percentage shown.

**Relay Log Evidence:** Coach start/stop routing correctly:
```
02:02:38 [ROUTE] App -> Robot: start_coach
02:02:38 [ROUTE] Robot -> App: coach_started
...multiple coach sessions observed...
```

**Root Cause:** ROBOT-side AI/TTS logic

**Who Needs to Fix:** ROBOT
- Voice feedback logic needs debugging
- Detection percentage overlay not being sent or not being generated

**Relay Action:** None - relay only routes messages, doesn't process coach logic.

---

### 6. Servo Motor / Camera Rotation

**Symptom:** Settings checkbox doesn't do anything, no movement observed

**Relay Log Evidence:** No servo commands observed in filtered logs

**Who Needs to Fix:** APP and/or ROBOT
- APP: Is checkbox sending servo commands?
- ROBOT: Is servo handler implemented?

**Relay Action:** None - relay routes servo commands, check if any are being sent.

---

## Summary Table

| Issue | Relay Status | Owner |
|-------|-------------|-------|
| Mission stuck initializing | Routing OK, data is null | **ROBOT** |
| MP3 413 error | Relay OK (20MB), robot rejecting | **ROBOT** |
| Missing /missions endpoint | 404 - endpoint doesn't exist | **RELAY or APP** |
| Schedule "failed" | Actually succeeded! | **APP** |
| Coach voice inconsistent | Routing OK | **ROBOT** |
| Servo not working | No commands seen | **APP/ROBOT** |

---

## Recommended Relay Changes

### Priority 1: Add /missions Endpoint (if needed)
```python
@router.get("/missions")
async def get_mission_types():
    """Return available mission types"""
    return {
        "missions": [
            {"id": "sit_training", "name": "Sit Training", "stages": 5},
            {"id": "down_training", "name": "Down Training", "stages": 5},
            # etc
        ]
    }
```

### Priority 2: Add Mission Data Validation/Logging
```python
# In websocket.py mission_progress handler
if not data.get("status") and not data.get("stage"):
    logger.warning(f"[MISSION] Empty progress data from {device_id}")
```

### Priority 3: Investigate Robot Upload Path
The MP3 413 is NOT from relay. Need to check:
- Does robot have HTTP endpoint for direct upload?
- What's the robot's web server config?
- Is there a `/api/music/upload` on the robot itself?

---

## Questions for Other Teams

### For Robot Team:
1. Why is `mission_progress` sending all null values?
2. Does the robot have its own HTTP server with upload limits?
3. What happened to the detection percentage overlay?
4. Is servo motor handler implemented?

### For App Team:
1. Why showing "failed to create schedule" when `schedule_created` was received?
2. What data do you expect from `GET /missions`?
3. Is the servo checkbox sending any commands?
4. Is the mission button state tied to `mission_started` event or something else?

---

## Next Steps

1. **ROBOT** should check mission_progress event population
2. **ROBOT** should check its own HTTP server size limits
3. **APP** should fix schedule_created handler
4. **RELAY** can add /missions endpoint if app needs it
5. **ALL** need to align on mission state machine (what events trigger what UI changes)
