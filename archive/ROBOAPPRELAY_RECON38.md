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

-- TO APPCLAUDE: #2 is your responsibilty.

-- APP CC: Done. Increased limit from 10MB to 20MB in `quick_actions.dart:312`. Also may need RELAY to increase HTTP body limit.

-- RELAY CC: DONE. Increased `MAX_FILE_SIZE` from 10MB to 20MB in `app/routers/music.py:31-32`. The HTTP upload endpoint now accepts files up to 20MB.

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

-- RELAY CC: Relay is correctly forwarding these commands. Confirmed via logs showing `Command: get_schedules` arriving at robot. No relay changes needed - this is purely a ROBOT handler issue.

-- TO APPCLAUDE: This is the robot's Are you sure you want to send via Websocket for this? vs using REST/HTTP?  We're going to need that pipeline for the Mp3 right? Why websocket vs http/rest?
Confirm and we can validate with robot side.

-- APP CC: Yes, WebSocket is correct for schedules. Here's why:

1. **Architecture**: App connects to RELAY (cloud), not directly to robot. Relay only forwards WebSocket commands. The REST endpoints in `api/server.py` are for LOCAL access only (same network as robot).

2. **The Flow**: `APP → WebSocket → RELAY → WebSocket → ROBOT`. Relay doesn't proxy REST to robot.

3. **Offline execution**: Schedules must live ON ROBOT for offline operation. WebSocket lets robot store locally.

**MP3 is different** - we use HTTP multipart to RELAY, then relay forwards to robot. But schedules need real-time bidirectional sync which WebSocket provides.

**Exact commands APP sends (already in `websocket_client.dart`):**
```dart
sendGetSchedules()        // command: 'get_schedules'
sendCreateSchedule(...)   // command: 'create_schedule'
sendUpdateSchedule(...)   // command: 'update_schedule'
sendDeleteSchedule(id)    // command: 'delete_schedule'
```

**APP side is correct. ROBOT needs to add handlers.**

-- RELAY CC: Confirmed. Relay forwards all commands transparently. The schedule commands flow through the existing `send_to_robot()` path. No relay modifications needed for scheduler.

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
-- TO APPCLAUDE: seems robot claude thinks this is app issue

-- APP CC: Partially. There are TWO limits:
1. **APP limit** (`quick_actions.dart:312`): Was 10MB, now increased to 20MB DONE
2. **RELAY limit**: FastAPI/uvicorn default ~2MB. RELAY Claude needs to increase this.

-- TO RELAYCLAUDE: do this please... seems you gotta up the limit to 20MB

-- RELAY CC: DONE. Fixed in `app/routers/music.py`:
- Line 31-32: Changed `MAX_FILE_SIZE = 10 * 1024 * 1024` to `MAX_FILE_SIZE = 20 * 1024 * 1024`
- Line 73: Updated docstring from "max 10MB" to "max 20MB"

NOTE: FastAPI/Starlette doesn't have a default body size limit for file uploads - we handle it ourselves in the `upload_music()` function by reading the file and checking `len(content) > MAX_FILE_SIZE`. The 413 error is returned by OUR code, not by uvicorn/FastAPI framework defaults.

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

-- RELAY CC: The "File too large for server" error was coming from relay's `app/routers/music.py` returning HTTP 413 when file exceeded `MAX_FILE_SIZE`. Now increased to 20MB to match app. If app pre-validates at 20MB and relay accepts 20MB, uploads should work.

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

-- RELAY CC: FastAPI/Starlette does NOT have a default body size limit for multipart uploads. The limit was in our own code (`MAX_FILE_SIZE` constant). Now fixed to 20MB.

**If using Nginx:**
```nginx
client_max_body_size 50M;
```
-- TO RELAYCLAUDE: seems you need to make sure this setting will work as well? above

-- RELAY CC: Nginx config is a DEPLOYMENT concern, not codebase. If relay is deployed behind nginx (e.g., for SSL termination), the sysadmin must add `client_max_body_size 50M;` to nginx config. This is NOT in the Python codebase. For AWS Lightsail direct deployment (no nginx), this is not needed. Added note to deployment docs below.

**DEPLOYMENT NOTE (for nginx setups):**
If using nginx as reverse proxy, add to nginx.conf or site config:
```nginx
server {
    ...
    client_max_body_size 50M;
    ...
}
```





### Build 38 Alternative Solution (Already Implemented)
Build 38 added `download_song` command - robot downloads from URL:
```python
elif command == 'download_song':
    # Robot downloads MP3 directly via HTTP GET
    # {"url": "https://...", "filename": "my_song.mp3"}
    # Max 20MB, 60s timeout
```

-- RELAY CC: The HTTP upload flow is:
1. App POSTs file to `POST /api/music/upload` on relay
2. Relay saves file to `/tmp/wimz-uploads/{file_id}_{filename}`
3. Relay sends WebSocket command `download_song` to robot with URL `/api/music/file/{file_id}`
4. Robot calls `GET /api/music/file/{file_id}` on relay to download the file
5. Robot saves to local music directory

This flow is already working. The only issue was the 10MB limit.

### Recommended Approach
1. **Short-term:** Increase relay HTTP body size limit to 20MB+ - DONE
2. **Long-term:** Use `download_song` - app uploads to S3/CDN, sends URL to robot

### NOT a Robot Issue
Robot can receive via:
- `upload_song` (base64 over WebSocket) - works for small files
- HTTP POST `/music/upload` (base64 in JSON body)
- `download_song` (URL, robot fetches directly) - preferred for large files

---
-- TO RELAYCLAUDE: Seems you could do a bit of code update on this as well?

-- RELAY CC: Code update DONE. The relay now accepts 20MB files. No other relay changes needed for MP3 upload.

## Issue 3: Coach Mode Issues

### Symptoms
1. Only says "Sit", then dog name, then "good" + treat too quickly
2. Too sensitive compared to before
3. Camera servo doesn't move at all
4. Request: Manual trick buttons from app
-- TO APPCLAUDE: got it? wire up those buttons on the coach screen to commands and send em?

-- APP CC: Done! Made the "Watching for:" behavior chips (SIT, DOWN, STAND) tappable. When tapped, sends `force_trick` command. Added touch icon to indicate tappable.

**Files changed:**
- `coach_screen.dart` - Added `_TrickChip` widget with `onTap` that calls `forceTrick()`
- `coach_provider.dart` - Added `forceTrick(trick)` method
- `websocket_client.dart` - Added `sendForceTrick(trick)` method

**Command sent:**
```json
{"command": "force_trick", "data": {"trick": "sit"}}
```

-- RELAY CC: No relay changes needed. The `force_trick` command will flow through existing WebSocket routing. Relay forwards all `command` type messages to robot transparently.

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

-- TO ROBOCLAUDE:  No, you misunderstand..... not sensitive to DETECTION, but sensitive to "tricks" like it's just giving out treats without the dog doing anything, this worked perfect for coach version with the xbox controller two weeks+ ago (Jan 14) but now it's too sensitive.  Let's just keep that nuanced a bit ok?  Not sure if you have a solution, but sure let's do detection time at 2 seconds, presence ratio to 55%....

-- RELAY CC: No relay involvement. This is purely robot-side coaching engine tuning.

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

-- RELAY CC: Relay is forwarding these commands correctly (robot logs show they arrive). ROBOT needs to add handlers.

#### C. Camera Servo Doesn't Move
`tracking_enabled = False` by default (pan_tilt.py:33)

Build 34 disabled auto-tracking in coach mode to prevent jerky motion. Build 38 added "nudge tracking" (slow, gentle movement) but it requires `tracking_enabled = True`.

**Current behavior:** Camera stays fixed in center position during coach mode.

**The nudge tracking code exists (pan_tilt.py:215-299) but may not be activating because `tracking_enabled` is never set to True.**j


-- TO APPCLAUDE: Can we put a toggle for settings "enable tracking" a true/false checkbox in the gui for this?  and then send this to change to robot claude??

-- APP CC: Done! Added "Camera Track Dog" toggle in Settings screen under new "Camera" section.

**Files changed:**
- `settings_provider.dart` - Added `cameraTrackingEnabled` field and `setCameraTrackingEnabled()` method
- `settings_screen.dart` - Added `_CameraTrackingTile` SwitchListTile widget
- `websocket_client.dart` - Added `sendSetTrackingEnabled(enabled)` method

**Command sent when toggled:**
```json
{"command": "set_tracking_enabled", "data": {"enabled": true}}
```

**ROBOT must add handler for `set_tracking_enabled` command.**

-- RELAY CC: No relay changes needed. Command flows through existing WebSocket routing.

#### D. Coaching Flow Seems Wrong
The flow "Sit" -> dog name -> "good" + treat is CORRECT but FAST:

1. **Dog detected** (1.5s presence)
2. **Trick selected** = "sit" (first in rotation)
3. **Command played** = "Sit" audio
4. **Watching** for pose...
5. **Greeting** = dog name (may play after command)
6. **Success** = pose detected -> "good" + treat

The flow is working but the 1.5s detection + fast pose detection = feels rushed.


-- TO ROBOCLAUDE: Not correct, how did you get this flow wrong? We had it right before, this is correct:
1. **Dog detected** (1.5s presence)  -- let's raise is to 2 seconds
2. **Trick selected** = "sit" (first in rotation) --- nothing should happen just selection of the trick here in the code and loading it.
3. **Greeting** = dog name (may play after command, but hopefully first you want it to play first it should be "dog name", then "command" audio.
3. **Command played** = "Sit" audio
4. **Watching** for pose...
6. **Success** = pose detected -> "good" + treat
7. **Failure** = Only If failure, Repeat same trick for one more loop. Then go back to coach mode waiting/time cooldown etc? we hae the logic for this already.

-- TO ROBOCLAUDE: You misunderstand. It's not "fast" it's wrong and the speed causes it to be wrong indirectly but not exclusively. ie not that the dog is present/sitting and boom done and the AI does everything fast and it bothers me. it's that the dog does not SIT, the app just says "dogs name" to the dog and starts the trick immediately and tehn spits the treat out even when the dog FAILS to perform the trick.  Again please reference the older settings in the coach mode we had and use those exactly... i'm not sure why you decided 1.5 seconds woudl be better, i don't think that was the issue, i think it was detecting tehe dog... in fact in coach mode i downloaded the videos finally and it shows it DID detect the dog but it didnt address the dog with tricks because of the other issues this is what i'm realizing.  So i think 2-3 seconds is fine, but it was the coordination issue that caused the problem....

-- RELAY CC: No relay involvement. This is robot-side coaching engine logic.

### Fix Required (ROBOT)

#### For Speed Issue:
Consider reverting detection settings:
```python
# coaching_engine.py lines 168-170
self.detection_time_sec = 2.5   # Compromise between 1.5 and 3.0
self.presence_ratio_min = 0.55  # Compromise between 0.50 and 0.66
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
-- TO APPCLAUDE: SOudns like robot claude will match your commands for stop_coach start_coach

-- APP CC: Confirmed. App already sends these commands (`coach_provider.dart:144` and `:162`). Robot just needs handlers.


#### For Servo Tracking:
Enable tracking when entering coach mode:
```python
# In coaching_engine.start() after line 227:
from services.motion.pan_tilt import get_pantilt_service
pantilt = get_pantilt_service()
pantilt.set_tracking_enabled(True)
```
-- TO APPCLAUDE: Yeah put a toggle for this in settings "camera track dog?" checkbox style or yes/no etc according to style guide, send over relay

-- APP CC: Done! See above. Toggle added to Settings screen.

-- TO ROBOCLAUDE: anticipate a command coming from this

-- APP CC: ROBOT must add handler:
```python
elif command == 'set_tracking_enabled':
    enabled = params.get('enabled', False)
    from services.motion.pan_tilt import get_pantilt_service
    pantilt = get_pantilt_service()
    pantilt.set_tracking_enabled(enabled)
    if self.relay_client:
        self.relay_client.send_event('tracking_enabled', {'enabled': enabled})
```

-- RELAY CC: Relay will forward the `tracking_enabled` event from robot back to app. Already supported via existing event routing.

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
-- TO APPCLAUDE: Yes create buttons that allow these tricks (the text visual UI is already present on the coach mode screen)

-- APP CC: Done! The behavior chips in "Watching for:" section are now tappable buttons. Tap "SIT" -> sends `force_trick` with `{"trick": "sit"}`.

-- TO ROBOCLAUDE: Anticipate commands to start these tricks directly arriving, just like we cycled through with the xbox controller in coach mode

-- RELAY CC: `force_trick` command will route through existing WebSocket forwarding. No relay changes needed.

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

-- TO APPCLAUDE: I think this is the glitch i was saying, i "start" the mission, but this status is basically invisible and the button reverts back to "Start mission" so it appears I didn't start the mission... then i do it a second time and it shows i am in a mission.... so am i starting a mission once? twice? and is it auto canceling not sure? but the first step is fixing the glitch where the UI keeps that Start mission, once clicked and executed as "Stop Mission" ui in the mission menus instead of reverting it instantly the first time.... i hope this makes sense, it soudns like robot claude is going to look at this too, but you have first move here to fix things....

-- APP CC: I understand the issue now. Fixed with multiple changes:

**1. Handle `failure_reason` from robot** (`missions_provider.dart`):
- When robot sends `action: 'failed'` with `failure_reason: 'mission_already_active'`, app now syncs to the EXISTING mission instead of clearing state.
- Shows error: "A mission is already active (sit_training)"

**2. Pre-check before starting** (`missions_provider.dart:startMission()`):
- If app already knows a mission is active, shows error immediately
- Prevents sending duplicate start commands

**3. Added `get_mission_status` request** (`missions_provider.dart:requestMissionStatus()`):
- Can be called when entering mission screen to sync UI with robot state
- Added `sendGetMissionStatus()` to websocket_client.dart

**4. Handle `mission_status` event**:
- When robot responds to `get_mission_status`, app syncs its state

**ROBOT needs to:**
1. Include `failure_reason: 'mission_already_active'` when start fails
2. Add handler for `get_mission_status` command that returns current state

-- RELAY CC: Relay correctly forwards mission events. The issue is:
1. Robot not sending `failure_reason` in response
2. Robot missing `get_mission_status` command handler
3. App not syncing state on screen entry

No relay changes needed. Events like `mission_progress`, `mission_status` already route through.

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
-- TO APPCLAUDE: Yeah if we're already in a mission and we try to start another mission we should handle that and say "already in mission".

-- APP CC: Done. App now handles this case and shows appropriate error message.

-- TO ROBOCLAUDE: Nope not exactly understanding what is going on yet, ok?  Let me explain. I start the app in "idle" mode. I click mission menu, then sit training, then "start mission" (dog profile already selected from manin screen to to clarify). I hear the "mission" audio. I see a text popup something like "sit mission started ~". THen the button i clicked to "start mission" is now green/back to "start mission" vs the "stop mission" the button UI should be as I already started the mission.

Then if i go back to the main screen next, i see "mission" in the UI for the app, but in the video overlay it says in text Mission 1/2 - Idle.  That should never show idle lol if you are in mission mode, you are assigned a mission.
In the alternative, if I stay and reclick the now green "Start mission" button again (instead of going back to main screen) now the mission ui button stays the red button with a "stop mission" button. I now exit out to the main menu, same problem. It shows I'm in "mission mode" in the UI, but in the video overlay text Mission 1/2 - Idle.. I hope this clarifies how we end up on the main screen with the "mission" mode present, the UI showing we are in the correct mission mode, but the backend, the video and the AI seemingly not able to do anything appropriate other than "detect" the dog, but not execution of the mission at all.

-- RELAY CC: This confirms the issue is robot-side state management. The relay is correctly forwarding:
- `start_mission` command to robot
- `mission_progress` event back to app

The problem is robot returning inconsistent state. Video overlay showing "MISSION - IDLE" means robot's overlay text generator is seeing `active_mission=None` even though mode is MISSION.

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
-- TO APPCLAUDE: Sure you probably should do this huh?

-- APP CC: Yes, implemented. Added `requestMissionStatus()` method and `mission_status` event handler. Robot needs to add `get_mission_status` command handler.

-- RELAY CC: Relay will forward `get_mission_status` command and route `mission_status` response event. No changes needed.

## Questions Answered

### Q: Why does coach mode only say "Sit"?
**A:** "Sit" is the first trick in the rotation. It's working correctly - the coaching engine picks tricks sequentially. If the session completes, the next session will say "down", then "spin", then "speak", then back to "sit".

### Q: Why does camera servo not move?
**A:** `tracking_enabled` is False by default. Build 34 disabled auto-tracking to prevent jerky motion. Build 38 added nudge tracking code but it's not being activated because tracking isn't enabled when entering coach mode.

### Q: What changed to make coach mode too sensitive?
**A:** Build 36 reduced detection time from 3.0s to 1.5s and presence ratio from 66% to 50% to fix slow detection. This made it feel "instant".

### Q: Can we add manual trick buttons from app?
**A:** Yes, need to add `force_trick` WebSocket command handler. The infrastructure exists (Xbox Guide button uses `_forced_trick`).
-- TO APPCLAUDE: of course... let' do this, easy win here.

-- APP CC: Done! Behavior chips are now tappable.

---

## Priority Fix Order

1. **Mission Mode** (CRITICAL) - Add failure reason OR auto-stop existing mission
2. **Scheduler** (HIGH) - Add WebSocket command handlers
3. **Coach Speed** (MEDIUM) - Tune detection_time_sec back up to 2.5s
4. **Camera Servo** (MEDIUM) - Enable tracking when entering coach mode
5. **start_coach/stop_coach** (LOW) - Add missing command handlers
6. **MP3 Upload** (APP ISSUE) - App needs to use download_song or increase limit

-- RELAY CC: MP3 Upload relay-side limit is now FIXED (20MB). App-side limit also fixed per APP CC.

---

## Files Modified (APP) - BUILD 38 FIXES

| File | Change |
|------|--------|
| `quick_actions.dart:312` | Increased MP3 upload limit 10MB -> 20MB |
| `websocket_client.dart` | Added `sendForceTrick()`, `sendSetTrackingEnabled()`, `sendGetMissionStatus()` |
| `coach_provider.dart` | Added `forceTrick(trick)` method |
| `coach_screen.dart` | Made behavior chips tappable with `_TrickChip` widget |
| `settings_provider.dart` | Added `cameraTrackingEnabled` setting |
| `settings_screen.dart` | Added "Camera Track Dog" toggle |
| `missions_provider.dart` | Handle `failure_reason`, `mission_status` events, pre-check active mission |

## Files Modified (RELAY) - BUILD 38 FIXES

| File | Change |
|------|--------|
| `app/routers/music.py:31-32` | Increased `MAX_FILE_SIZE` from 10MB to 20MB |
| `app/routers/music.py:73` | Updated docstring to reflect 20MB limit |

-- RELAY CC: That's it. Only the upload size limit needed changing on relay side. All WebSocket command routing already works.

## Files to Modify (ROBOT)

| File | Changes |
|------|---------|
| `main_treatbot.py` | Add: get_schedules, create_schedule, start_coach, stop_coach, force_trick, set_tracking_enabled, get_mission_status handlers |
| `orchestrators/coaching_engine.py` | Tune detection_time_sec, enable tracking on start |
| `orchestrators/mission_engine.py` | Return failure reason in start_mission response |

---

## Command Reference for ROBOT

### New Commands APP Now Sends:

```python
# force_trick - Force a specific trick in coach mode
params = {'trick': 'sit'}  # or 'down', 'stand'
# Response: trick_forced event

# set_tracking_enabled - Enable/disable camera tracking
params = {'enabled': True}
# Response: tracking_enabled event

# get_mission_status - Request current mission state
params = {}
# Response: mission_status event with {active: bool, mission_id: str, ...}
```

-- RELAY CC: All these commands route through relay's existing WebSocket forwarding. Robot receives them in `_handle_cloud_command()`. Robot sends response events via `relay_client.send_event()` which relay forwards to app.

---

## RELAY SUMMARY

### Changes Made:
1. **`app/routers/music.py`** - Increased `MAX_FILE_SIZE` from 10MB to 20MB

### No Changes Needed For:
- WebSocket command routing (already works for all commands)
- Event forwarding (already works for all events)
- Schedule commands (relay forwards transparently)
- Coach commands (relay forwards transparently)
- Mission commands (relay forwards transparently)

### Deployment Note:
If using nginx as reverse proxy, add to nginx config:
```nginx
client_max_body_size 50M;
```

---

*Report annotated by APP CC, RELAY CC. Ready for ROBOT implementation.*
