# Build 38 Test Reconnaissance Report
**Date:** 2026-02-01
**Tester:** Morgan
**Test Window:** 15:50 - 16:35 robot time (20:50 - 21:35 relay time)

---

## Summary

| Issue | Severity | Owner | Root Cause |
|-------|----------|-------|------------|
| 1. Scheduler fails | HIGH | ROBOT | WebSocket handlers missing |
| 2. MP3 upload fails | MEDIUM | APP | App-side size limit (not relay) |
| 3. Coach mode too sensitive | MEDIUM | ROBOT | Build 36 detection time halved |
| 4. Mission mode fails | CRITICAL | ROBOT | Race condition - mission already active |

---

## Issue 1: Scheduler Fails

### Symptoms
- "Timeout waiting for status from Robot"
- "Failed to create schedule"

### Evidence from Robot Logs (16:18:18)
```
2026-02-01 16:18:18,759 - RelayClient - INFO - Command: get_schedules, params: {...}
2026-02-01 16:18:18,760 - TreatBotMain - INFO - Event data: {'command': 'get_schedules', ...}
```
**No response sent back to relay.** Command received but not handled.

### Root Cause
**WebSocket command handlers for schedule commands DO NOT EXIST.**

The robot has REST API endpoints at `/schedules` (api/server.py lines 4137-4229):
- `GET /schedules` - list schedules
- `POST /schedules` - create schedule
- `GET /schedules/{id}` - get schedule
- `PUT /schedules/{id}` - update schedule
- `DELETE /schedules/{id}` - delete schedule

**But there are NO WebSocket command handlers in main_treatbot.py for:**
- `get_schedules`
- `create_schedule`
- `update_schedule`
- `delete_schedule`
- `get_scheduler_status`

The app sends commands via WebSocket -> Relay -> Robot, but robot doesn't handle them.

### Fix Required (ROBOT)
Add WebSocket command handlers in `main_treatbot.py` `_handle_cloud_command()`:

```python
elif command == 'get_schedules':
    from core.schedule_manager import get_schedule_manager
    manager = get_schedule_manager()
    schedules = manager.list_schedules()
    if self.relay_client:
        self.relay_client.send_event('schedules_list', {'schedules': schedules})

elif command == 'create_schedule':
    from core.schedule_manager import get_schedule_manager
    manager = get_schedule_manager()
    result = manager.create_schedule(params)
    if self.relay_client:
        self.relay_client.send_event('schedule_created', result)

elif command == 'get_scheduler_status':
    from core.mission_scheduler import get_mission_scheduler
    scheduler = get_mission_scheduler()
    if self.relay_client:
        self.relay_client.send_event('scheduler_status', scheduler.get_status())
```

---

## Issue 2: MP3 Uploader Fails

### Symptoms
- App shows "File too large for server"

### Evidence from Relay Logs (20:50)
```
Feb 01 20:50:12 - [ROUTE] App(user_000003) -> Robot(wimz_robot_01): audio_toggle
Feb 01 20:50:18 - [ROUTE] App(user_000003) -> Robot(wimz_robot_01): audio_next
```
**No upload attempt visible in relay logs at 20:50.**

### Clarification: Using HTTP (Not WebSocket)
The app is attempting HTTP upload, not WebSocket. This means the issue is likely:

1. **Relay HTTP size limit** - FastAPI/uvicorn default body limit
2. **Nginx/proxy size limit** - If relay is behind a proxy
3. **App-side pre-check** - App checking size before upload attempt

### Where is the limit?
Since relay logs show NO upload attempt, the error "File too large for server" is being generated BEFORE the request reaches the relay. This could be:
- App-side size validation
- iOS/network layer rejection
- Relay returning 413 (Request Entity Too Large) without logging

### Fix Required (RELAY)
Check and increase HTTP body size limits:

**FastAPI/Starlette:**
```python
# In relay server startup
app = FastAPI()
# Default is ~2MB, increase to 50MB
from starlette.config import Config
# Or use middleware to allow larger uploads
```

**If using Nginx:**
```nginx
client_max_body_size 50M;
```

### Build 38 Alternative Solution (Already Implemented)
Build 38 added `download_song` command - robot downloads from URL:
```python
elif command == 'download_song':
    # Robot downloads MP3 directly via HTTP GET
    # {"url": "https://...", "filename": "my_song.mp3"}
    # Max 20MB, 60s timeout
```

### Recommended Approach
1. **Short-term:** Increase relay HTTP body size limit to 20MB+
2. **Long-term:** Use `download_song` - app uploads to S3/CDN, sends URL to robot

### NOT a Robot Issue
Robot can receive via:
- `upload_song` (base64 over WebSocket) - works for small files
- HTTP POST `/music/upload` (base64 in JSON body)
- `download_song` (URL, robot fetches directly) - preferred for large files

---

## Issue 3: Coach Mode Issues

### Symptoms
1. Only says "Sit", then dog name, then "good" + treat too quickly
2. Too sensitive compared to before
3. Camera servo doesn't move at all
4. Request: Manual trick buttons from app

### Evidence from Robot Logs
```
2026-02-01 15:51:59,684 - TreatBotMain - WARNING - Unknown cloud command: start_coach
2026-02-01 15:54:20,329 - TreatBotMain - WARNING - Unknown cloud command: stop_coach
```

### Root Cause Analysis

#### A. "Too Sensitive" / "Too Fast"
**Build 36 reduced detection requirements by 50%:**

| Setting | Before (Build 35) | After (Build 36) | Location |
|---------|-------------------|------------------|----------|
| `detection_time_sec` | 3.0s | 1.5s | coaching_engine.py:169 |
| `presence_ratio_min` | 66% | 50% | coaching_engine.py:170 |

**Why it was changed:** Build 35 testing showed detection taking 60+ seconds. Fix was to reduce requirements.

**Result:** Dog is now eligible for coaching session after only 1.5 seconds at 50% presence, which feels "instant" compared to before.

#### B. Missing Commands: `start_coach` / `stop_coach`
These WebSocket command handlers DO NOT EXIST in main_treatbot.py.

The app sends:
```json
{"command": "start_coach", "params": {...}}
{"command": "stop_coach", "params": {...}}
```

Robot logs show:
```
WARNING - Unknown cloud command: start_coach
WARNING - Unknown cloud command: stop_coach
```

**The app is using set_mode to change mode, but also sending start_coach/stop_coach which aren't handled.**

#### C. Camera Servo Doesn't Move
`tracking_enabled = False` by default (pan_tilt.py:33)

Build 34 disabled auto-tracking in coach mode to prevent jerky motion. Build 38 added "nudge tracking" (slow, gentle movement) but it requires `tracking_enabled = True`.

**Current behavior:** Camera stays fixed in center position during coach mode.

**The nudge tracking code exists (pan_tilt.py:215-299) but may not be activating because `tracking_enabled` is never set to True.**

#### D. Coaching Flow Seems Wrong
The flow "Sit" -> dog name -> "good" + treat is CORRECT but FAST:

1. **Dog detected** (1.5s presence)
2. **Trick selected** = "sit" (first in rotation)
3. **Command played** = "Sit" audio
4. **Watching** for pose...
5. **Greeting** = dog name (may play after command)
6. **Success** = pose detected -> "good" + treat

The flow is working but the 1.5s detection + fast pose detection = feels rushed.

### Fix Required (ROBOT)

#### For Speed Issue:
Consider reverting detection settings:
```python
# coaching_engine.py lines 168-170
self.detection_time_sec = 2.5   # Compromise between 1.5 and 3.0
self.presence_ratio_min = 0.60  # Compromise between 0.50 and 0.66
```

#### For start_coach/stop_coach:
Add handlers in main_treatbot.py:
```python
elif command == 'start_coach':
    from orchestrators.coaching_engine import get_coaching_engine
    engine = get_coaching_engine()
    started = engine.start()
    if self.relay_client:
        self.relay_client.send_event('coach_started', {'success': started})

elif command == 'stop_coach':
    from orchestrators.coaching_engine import get_coaching_engine
    engine = get_coaching_engine()
    engine.stop()
    if self.relay_client:
        self.relay_client.send_event('coach_stopped', {'success': True})
```

#### For Servo Tracking:
Enable tracking when entering coach mode:
```python
# In coaching_engine.start() after line 227:
from services.motion.pan_tilt import get_pantilt_service
pantilt = get_pantilt_service()
pantilt.set_tracking_enabled(True)
```

### Request: Manual Trick Buttons
**Currently Supported:** Xbox controller can force tricks via Guide button.

**To Add App Support:** Need new WebSocket command `force_trick`:
```python
elif command == 'force_trick':
    trick = params.get('trick')  # 'sit', 'down', 'stand'
    from orchestrators.coaching_engine import get_coaching_engine
    engine = get_coaching_engine()
    engine._forced_trick = trick
    engine._start_session_for_dog(dog_id)  # Need to expose this method
```

---

## Issue 4: Mission Mode CRITICAL FAILURE

### Symptoms
- Click "Start Mission" -> hears "Mission mode enabled"
- UI immediately shows "Enable mission mode" button (reset)
- Video overlay shows "MISSION - IDLE" not "MISSION - SIT"

### Evidence from Robot Logs

**16:22:13 - Robot already in MISSION mode:**
```
DetectorService - INFO - Detection loop alive: mode=SystemMode.MISSION
```

**16:23:58 - User clicks "Start Mission" (sit_training):**
```
RelayClient - INFO - Command: start_mission, params: {'mission_id': 'sit_training'}
orchestrators.mission_engine - INFO - [MISSION] start_mission called: name=sit_training
orchestrators.mission_engine - ERROR - Mission already active
TreatBotMain - INFO - Start mission 'sit_training' -> False
```

**16:34:58 - User disconnects, mission stops:**
```
RelayClient - INFO - Stopping active mission due to user disconnect
StateManager - INFO - Mode changed: mission -> idle (Mission stopped: user_requested)
```

### Root Cause
**A mission was ALREADY running when user clicked "Start Mission".**

The `start_mission` call failed with "Mission already active" and returned `False`. The app received `action: 'failed'` in the response and correctly showed the "Enable" button because the start failed.

**Question: How did the mission get started before the user clicked "Start Mission"?**

Looking at the logs, the robot was in MISSION mode from at least 16:22:13 (before user clicked start at 16:23:58). This means:
1. Mission was started earlier (before 16:22)
2. User navigated away and came back
3. User clicked "Start Mission" again but mission was still running
4. Start failed, UI reset to "Enable" state

### The Real Bug
When user enters Mission Mode screen in the app, they expect to START a mission. But if a mission is already running (from a previous session, or auto-scheduled), clicking "Start" fails.

**The robot sends `action: 'failed'` but doesn't explain WHY:**
```python
self.relay_client.send_event('mission_progress', {
    'action': 'started' if started else 'failed',  # Just 'failed', no reason
    'mission': mission_name,
    **status
})
```

### Fix Required (ROBOT)

#### 1. Add failure reason to response:
```python
elif command == 'start_mission':
    engine = get_mission_engine()
    started = engine.start_mission(mission_name, dog_id=dog_id)
    status = engine.get_mission_status()
    if self.relay_client:
        self.relay_client.send_event('mission_progress', {
            'action': 'started' if started else 'failed',
            'mission': mission_name,
            'failure_reason': 'mission_already_active' if not started and status.get('active') else None,
            **status
        })
```

#### 2. Or auto-stop existing mission before starting new one:
```python
elif command == 'start_mission':
    engine = get_mission_engine()
    # If mission already running, stop it first
    if engine.active_session:
        engine.stop_mission(reason='new_mission_requested')
    started = engine.start_mission(mission_name, dog_id=dog_id)
    ...
```

#### 3. App should check mission status before showing "Start" button
The app should call `mission_status` when entering the mission screen and show appropriate UI:
- If mission active: Show "Stop Mission" and current progress
- If no mission: Show "Start Mission"

---

## Questions Answered

### Q: Why does coach mode only say "Sit"?
**A:** "Sit" is the first trick in the rotation. It's working correctly - the coaching engine picks tricks sequentially. If the session completes, the next session will say "down", then "spin", then "speak", then back to "sit".

### Q: Why does camera servo not move?
**A:** `tracking_enabled` is False by default. Build 34 disabled auto-tracking to prevent jerky motion. Build 38 added nudge tracking code but it's not being activated because tracking isn't enabled when entering coach mode.

### Q: What changed to make coach mode too sensitive?
**A:** Build 36 reduced detection time from 3.0s to 1.5s and presence ratio from 66% to 50% to fix slow detection. This made it feel "instant".

### Q: Can we add manual trick buttons from app?
**A:** Yes, need to add `force_trick` WebSocket command handler. The infrastructure exists (Xbox Guide button uses `_forced_trick`).

---

## Priority Fix Order

1. **Mission Mode** (CRITICAL) - Add failure reason OR auto-stop existing mission
2. **Scheduler** (HIGH) - Add WebSocket command handlers
3. **Coach Speed** (MEDIUM) - Tune detection_time_sec back up to 2.5s
4. **Camera Servo** (MEDIUM) - Enable tracking when entering coach mode
5. **start_coach/stop_coach** (LOW) - Add missing command handlers
6. **MP3 Upload** (APP ISSUE) - App needs to use download_song or increase limit

---

## Files to Modify

| File | Changes |
|------|---------|
| `main_treatbot.py` | Add: get_schedules, create_schedule, start_coach, stop_coach, force_trick handlers |
| `orchestrators/coaching_engine.py` | Tune detection_time_sec, enable tracking on start |
| `orchestrators/mission_engine.py` | Return failure reason in start_mission response |
