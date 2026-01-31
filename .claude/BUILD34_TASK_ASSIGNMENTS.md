# Build 34 Task Assignments

**Based on:** Build 33 Testing Session (01/31/2026 01:56-02:59)
**Logs:** Located in `logs/` folder

---

## 🤖 ROBOT CLAUDE - Primary Focus

### P0: Mission Execution Pipeline (CRITICAL)

**Problem:** Missions don't actually execute. UI shows mission mode but robot does nothing.

**Symptoms:**
- Puppy Basics / Obedience / Calm → Play shows 0%, Stop immediately, nothing runs
- Sit Training → Enters mission mode, audio plays, but AI does nothing even with dog detected
- Down/Stay Training → Same - "No active mission" right after starting
- Robot stays in IDLE even when app shows MISSION mode

**Root Cause Investigation Needed:**
1. Is `start_mission` command being received?
2. Is mission_engine actually starting?
3. Is DetectorService running during mission?
4. Why does it work partially for Sit but not others?

**Logging to add:**
```python
logger.info(f"[MISSION] Received start_mission: {mission_name}")
logger.info(f"[MISSION] Mission engine state: {self.mission_engine.state}")
logger.info(f"[MISSION] Detector running: {self.detector_service.is_running}")
logger.info(f"[MISSION] Current stage: {self.mission_engine.current_stage}")
```

---

### P0: AI Detection / Dog Identification (CRITICAL)

**Problem:** AI detection regressed badly from previous builds.

**Symptoms:**
- Only "Elsa" ever appears with green box
- Bezik NEVER detected even when clearly visible
- User's ARM was detected as "Elsa" while actual dog ignored
- "Dog detected 50%" but wrong dog labeled

**Questions to Answer (Need MD file):**
1. How does dog identification currently work?
2. Is it ARUCO-based or vision-based?
3. Do we have trained data for Bezik/Elsa or just ARUCO assignment?
4. What changed between working version and now?

**Logic Fix Required:**
```python
# If no ARUCO tag identified:
if not aruco_detected:
    dog_name = "Dog"  # Generic, not "Elsa"
    
# Only use specific name if:
# 1. ARUCO tag matches known dog, OR
# 2. Vision model confident about specific dog

# Allow training for ANY detected dog, identified or not
```

**Action:** Compare current detection code to Build 27-28 when it worked better.

---

### P1: Servo Control - Coach Mode (CRITICAL)

**Problem:** Servo movement is dangerously fast and jerky.

**Symptoms:**
- Camera aimed at ceiling
- Extremely fast, uncontrolled motion
- Jerky movements

**Fixes Required:**
1. **Dramatically slow movement speed** (reduce by 50-75%)
2. **Add motion smoothing** (interpolation/easing)
3. **Limit max angles** to ~50% of current range
4. **DISABLE auto-servo in Coach Mode** until fixed

**Code Changes:**
```python
# Reduce servo speed
SERVO_SPEED_LIMIT = 0.3  # Was probably 1.0

# Add angle limits
PAN_MIN, PAN_MAX = -45, 45    # Was probably -90, 90
TILT_MIN, TILT_MAX = -30, 30  # Was probably -60, 60

# Add smoothing
def smooth_servo_move(current, target, smoothing=0.1):
    return current + (target - current) * smoothing
```

**Recommendation:** Find and restore previous servo tracking logic.

---

### P1: Mode/State Synchronization

**Problem:** Robot mode doesn't match what app thinks.

**Symptoms:**
- App shows IDLE while robot in Manual
- Mission mode on app but robot stays IDLE
- Mode flips Manual → Idle → Manual

**Investigation:**
1. Is robot sending `mode_changed` events?
2. Are events reaching relay?
3. Is app receiving and processing them?

**Add logging:**
```python
# On every mode change
logger.info(f"[MODE] Changed: {old_mode} → {new_mode}, locked={locked}")
# Broadcast immediately
await self.broadcast_mode_change(new_mode, old_mode, locked)
```

---

### P2: Video Overlay Cleanup

**Problem:** Shows "???? Manual Mode"

**Fix:** Remove the ???? characters, display clean "Manual Mode" text only.

---

## 📱 APP CLAUDE - Secondary Focus

### P0: Mission UI State Sync

**Problem:** Mission UI doesn't reflect actual robot state.

**Symptoms:**
- Shows "Stop Mission" but robot never started
- Shows "completed" when nothing ran
- "No active mission" right after starting one
- Large "Waiting for Dog" overlay (needs 60% size reduction)

**Fixes:**
1. Poll `/missions/status` after starting to confirm it actually started
2. If robot returns `active: false`, show error "Mission failed to start"
3. Reduce "Waiting for Dog" overlay size by 60%
4. Don't show mission UI until confirmed running

```dart
Future<void> startMission(String name) async {
  final response = await http.post(.../missions/start);
  
  // Verify it actually started
  await Future.delayed(Duration(milliseconds: 500));
  final status = await http.get(.../missions/status);
  
  if (!status['active']) {
    showError("Mission failed to start on robot");
    return;
  }
  
  // Now show mission UI
  setState(() => _missionActive = true);
}
```

---

### P0: Mode Display Issues

**Problem:** Mode status frequently wrong.

**Symptoms:**
- Screen shows IDLE while robot in Manual
- Manual turns green but says Idle
- Flipping between modes

**Fixes:**
1. **Trust WebSocket `mode_changed` events** as source of truth
2. **Don't allow local state to override** server state
3. **Add debounce** to prevent rapid flipping
4. Consider: Remove mode selector from home screen, keep only in Drive screen

```dart
// On mode_changed event
void _handleModeChange(Map data) {
  final newMode = data['mode'];
  final locked = data['locked'] ?? false;
  
  // Always trust server state
  setState(() {
    _currentMode = newMode;
    _modeLocked = locked;
  });
}
```

---

### P1: MP3 Upload Breaking Connection (CRITICAL)

**Problem:** Selecting MP3 file triggers disconnect.

**Symptoms:**
- File picker opens ✓
- Select MP3 → window closes
- Bottom says "uploading"
- Top says "disconnected / reconnecting"
- Upload never completes

**Root Cause:** File selection itself triggers failure. Likely:
1. Large file blocks main thread
2. WebSocket times out during file read
3. Base64 encoding blocks UI

**Fix:**
```dart
Future<void> uploadMP3(File file) async {
  // Read file in isolate to not block main thread
  final bytes = await compute(file.readAsBytesSync, file.path);
  
  // Check file size
  if (bytes.length > 10 * 1024 * 1024) {  // 10MB limit
    showError("File too large");
    return;
  }
  
  // Encode in chunks if needed
  final base64 = base64Encode(bytes);
  
  // Send with timeout handling
  try {
    await _webSocket.send(jsonEncode({
      'type': 'command',
      'command': 'upload_song',
      'data': {
        'filename': file.name,
        'data': base64,
        'format': 'mp3'
      }
    }));
  } catch (e) {
    // Don't let upload failure kill connection
    showError("Upload failed: $e");
  }
}
```

---

### P1: Dog Profile Photo Cache Bug

**Problem:** Photo changes don't visually update.

**Symptoms:**
- First change works
- Subsequent changes: "photo updated" but UI shows old photo
- Only refreshes after full app restart

**Root Cause:** UI cache not invalidating.

**Fix:**
```dart
// After successful photo upload
void _onPhotoUpdated(String profileId, String newPhotoPath) {
  // Clear image cache for this profile
  imageCache.clear();
  
  // Force rebuild with cache-busting
  final cacheBuster = DateTime.now().millisecondsSinceEpoch;
  final newUrl = "$newPhotoPath?v=$cacheBuster";
  
  // Update provider state
  ref.invalidate(dogProfileProvider(profileId));
  
  // Force UI refresh
  setState(() {
    _photoUrl = newUrl;
  });
}
```

---

### P2: "Waiting for robot online" Flash

**Problem:** Briefly shows "waiting for robot" even when connected.

**Fix:** Add delay before showing disconnect message, check connection state properly.

---

### P2: Training Scheduler Save

**Problem:** Save button does nothing, can't save schedules.

**Fix:** Implement save functionality - wire up button to API call.

---

## 🌐 RELAY CLAUDE - Lower Priority

### P1: Connection Stability ✅ IMPLEMENTED

**Problem:** Many actions cause momentary disconnect.

**Symptoms:**
- MP3 upload triggers disconnect
- Mode changes sometimes cause reconnect
- "Waiting for robot online" appearing randomly

**Investigation Results:**
1. ✅ WebSocket timeout settings existed but weren't applied to Uvicorn
2. ✅ Large messages logged but no warning for problematic sizes
3. ✅ Added connection health monitoring

**Implementation (Build 34):**

1. **Updated `app/config.py`** - Added new settings:
   - `ws_max_message_size: int = 20 * 1024 * 1024` (20MB for MP3s)
   - `ws_ping_interval: int = 30`
   - `ws_ping_timeout: int = 20`

2. **Updated `run.py`** - Now passes ping settings to Uvicorn:
   ```python
   uvicorn.run(
       "app.main:app",
       ws_ping_interval=30,  # Keeps connection alive
       ws_ping_timeout=20,   # Detects dead connections
   )
   ```

3. **Updated `app/routers/websocket.py`** - Enhanced large message monitoring:
   - Added `LARGE_MESSAGE_THRESHOLD = 5MB`
   - Messages >5MB trigger warning: `[LARGE-MSG] ... may affect connection stability`
   - Helps identify problematic uploads

4. **Updated `.env.example`** - Documented new settings

**Note for APP CLAUDE:** The MP3 disconnect issue is likely client-side. The relay can handle 20MB messages. The app should:
- Read file in isolate (not main thread)
- Keep WebSocket heartbeat active during encode
- Handle upload timeouts gracefully

---

### P2: Event Forwarding Verification ✅ IMPLEMENTED

**Events tracked:** `mission_progress`, `mode_changed`, `dog_detected`, `treat_dispensed`

**Implementation (Build 34):**

Updated `app/routers/websocket.py` - Enhanced event forwarding with delivery verification:

```python
# Forward event and verify delivery
delivered_count = await manager.forward_event_to_owner(device_id, message)
if event_name in TRACKED_EVENTS:
    if delivered_count > 0:
        logger.info(f"[EVENT-OK] {event_name} delivered to {delivered_count} app(s)")
    else:
        logger.warning(f"[EVENT-FAIL] {event_name} NOT delivered - no apps connected")
```

**New log patterns to watch for:**
- `[EVENT-OK] mode_changed delivered to 1 app(s)` - Success
- `[EVENT-FAIL] mission_progress NOT delivered` - App not connected
- `[EVENT] dog_detected from device123: {...}` - Event received from robot
- `[MISSION] Progress event from device123: status=running stage=2/5`

**Verification checklist:**
- ✅ `mission_progress` - Enhanced logging with status/stage details
- ✅ `mode_changed` - Logged with data payload
- ✅ `dog_detected` - Logged with data payload
- ✅ `treat_dispensed` - Logged with data payload

**If events show `[EVENT-FAIL]`:** Check that app WebSocket is connected before robot sends events.

---

## Summary by Priority

### 🔴 P0 - Must Fix (Build 34)

| Issue | Owner | Description |
|-------|-------|-------------|
| Mission execution | Robot | Missions don't run, robot stays IDLE |
| AI detection regression | Robot | Wrong dog labeled, Bezik never seen |
| Mission UI sync | App | Shows wrong state, "completed" when nothing ran |
| Mode display | App | Shows IDLE when actually in Manual/Mission |

### 🟡 P1 - Should Fix (Build 34)

| Issue | Owner | Description |
|-------|-------|-------------|
| Servo control | Robot | Too fast, jerky, aims at ceiling |
| Mode state sync | Robot | Events not being sent/received properly |
| MP3 upload disconnect | App | File selection kills connection |
| Photo cache | App | Changes don't show until restart |
| ~~Connection stability~~ | ~~Relay~~ | ~~Random disconnects~~ ✅ DONE |

### 🟢 P2 - Nice to Have

| Issue | Owner | Description |
|-------|-------|-------------|
| Video overlay ???? | Robot | Remove emoji characters |
| "Waiting for robot" flash | App | False disconnect message |
| Scheduler save | App | Button does nothing |
| ~~Event forwarding logs~~ | ~~Relay~~ | ~~Verify events flow through~~ ✅ DONE |

---

## Requested: AI Detection Analysis Document

**Robot Claude:** Create a markdown file explaining:

1. **Current Implementation**
   - How does dog detection work now?
   - How does dog identification work?
   - ARUCO vs vision-based identification
   - What data exists for Bezik/Elsa?

2. **What Changed**
   - Compare to Build 27-28 code
   - What broke the detection?
   - Why is Bezik never detected?

3. **Fix Proposal**
   - How to properly handle unidentified dogs
   - How to support multi-dog scenarios
   - Recommended logic flow

---

## Testing Focus for Build 34

After fixes, test in this order:
1. Start Sit Training mission → verify robot enters mission mode AND AI activates
2. Check if dog detection shows correct dog (or "Dog" if unknown)
3. Verify mode changes sync between app and robot
4. Test MP3 upload without disconnect
5. Change dog photo twice → verify both changes visible

---

*Build 34 - Fix mission execution and AI detection regression*
