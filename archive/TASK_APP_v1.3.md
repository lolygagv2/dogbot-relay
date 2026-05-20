# APP CLAUDE — Task Batch v1.3

> Coordinated update. Read API_CONTRACT_v1_3.md first — it is the source of truth.
> This batch covers: always-on audio streaming, mode/UX restructure, UI layout fixes, and diagnostic audit.

---

## TASK 1: Always-On Audio Streaming (App Side)

### What
Receive and play the robot's WebRTC audio track. Add a mute/unmute toggle. Remove the "Tap to Listen" button entirely.

### Requirements

1. **Accept audio track from WebRTC:**
   - The robot's SDP offer will now include both video and audio tracks.
   - The app's SDP answer must accept both tracks.
   - Route the received audio track to the device speaker / earpiece.
   - Audio should begin playing as soon as the WebRTC connection is established (subject to mute state).

2. **Mute/Unmute toggle:**
   - Small speaker icon overlaid on the video feed (bottom-left or bottom-right corner).
   - Unmuted state: speaker icon, audio plays.
   - Muted state: speaker icon with slash, audio does not play.
   - Toggle is purely app-side. Do NOT send any command to the robot when toggling. Just enable/disable local audio playback.
   - Style reference: YouTube/Instagram mute button — small, unobtrusive, single tap.

3. **Mute state persistence:**
   - Save mute preference to local storage (SharedPreferences on Android, UserDefaults on iOS).
   - On app launch / WebRTC connect, restore saved preference.
   - **Default on first use: MUTED.** User must explicitly unmute to hear audio.

4. **Behavior when robot mic is server-side muted (AI modes):**
   - The robot sends silence during silent_guardian/coach/mission modes.
   - The app does not need to do anything special — it will just hear silence.
   - The mute toggle remains functional and visible. If the user unmutes during an AI mode, they just hear silence. When the robot returns to idle/manual, audio resumes automatically.

5. **Remove old audio UI:**
   - Remove the "Tap to Listen" button entirely from both portrait and landscape screens.
   - Keep the "Hold to Talk" (PTT) button but relocate it (see Task 3 below).

### Testing
- Connect to robot in idle mode, unmute → should hear ambient audio.
- Mute → audio stops immediately, no command sent to robot.
- Switch robot to coach mode while unmuted → audio goes silent (robot is muting its mic).
- Switch back to idle → audio resumes.
- Kill app, reopen → mute state should be preserved from last session.
- First install → should default to muted.

---

## TASK 2: Mode/UX Restructure

### What
Replace the current mode dropdown system with context-aware mode selection. Implement portrait/landscape mode rules and the "restore previous mode" behavior.

### Portrait Screen

1. **Mode dropdown (top-right, where current IDLE pill is):**
   - Options: **Idle / Guardian / Coach** — only these three.
   - Remove Manual and Mission from this dropdown.
   - Style: keep the current pill/dropdown style but make it slightly more prominent — it should look tappable, not just like a status label.
   - When Guardian or Coach is selected, show a brief confirmation on the video feed (e.g., "Guardian Active" or "Coach Active — Looking for dog..." banner that fades after 2 seconds).

2. **Drive button:**
   - Remains on the home screen as a prominent standalone button.
   - On tap: Store current portrait mode as `previous_portrait_mode` in app state. Send `set_mode` with `{"mode": "manual", "source": "drive_enter"}`. Transition to landscape.
   - The `previous_portrait_mode` value should be one of: idle, silent_guardian, coach.

3. **Exiting landscape (back arrow / back gesture):**
   - Retrieve `previous_portrait_mode` from app state.
   - Send `set_mode` with `{"mode": "<previous>", "source": "drive_exit_restore"}`.
   - Transition to portrait.
   - If `previous_portrait_mode` was not set (edge case), default to idle.

### Landscape Screen

4. **Mode selector (top-right area, near emergency stop):**
   - Show current mode as a small pill/chip.
   - Tappable to open selector with options: **Manual / Coach / Mission** (Mission only shown if a mission is loaded/active).
   - Selecting Manual: stays in landscape, sends `set_mode` with `{"mode": "manual", "source": "landscape_selector"}`. High-res video, no AI.
   - Selecting Coach: stays in landscape, sends `set_mode` with `{"mode": "coach", "source": "landscape_selector"}`. Switches to 640x640 + AI. User retains drive controls.
   - Selecting Mission: stays in landscape if a mission is loaded, sends `set_mode` with `{"mode": "mission", "source": "landscape_selector"}`.
   - Guardian is NOT available in landscape. If needed, user exits to portrait first.

5. **Mission entry flow:**
   - From portrait: User browses mission list → taps mission detail → taps "Start Mission".
   - App stores current portrait mode as `previous_portrait_mode`.
   - App sends `set_mode` with `{"mode": "mission", "source": "mission_start"}`.
   - App transitions to landscape with mission HUD overlay (current step, progress, trick queue).

6. **Mission exit (complete / failed / cancelled):**
   - Mission complete: Show "Mission Complete" screen. Tap Done → restore `previous_portrait_mode`, return to portrait.
   - Mission failed: Show "Mission Failed" screen with reason. Tap Done → same restore behavior.
   - User cancels (X button): Show "End Mission?" confirmation. Confirm → send `set_mode` with `{"mode": "<previous_portrait_mode>", "source": "mission_end"}`. Return to portrait.

7. **set_mode command format (all mode changes):**
   - Always include `source` and `timestamp` in the data payload:
   ```json
   {
     "type": "command",
     "command": "set_mode",
     "data": {
       "mode": "coach",
       "source": "dropdown",
       "timestamp": "2026-02-22T10:00:00Z"
     }
   }
   ```

### Coach Mode Specifics

8. **Coach in portrait:**
   - Robot watches for dog, picks from 3 random tricks, executes autonomously.
   - Show status on video overlay: "Looking for dog..." / "Trick: Sit" / etc.
   - Optionally show 3 quick-trick buttons the user can tap to manually trigger a specific trick.

9. **Coach in landscape:**
   - Same AI behavior as portrait coach — robot watches for dog and runs tricks.
   - User has full drive controls to manually position the robot.
   - Mode selector shows "Coach" as active.
   - This is the key workflow: drive to dog → coach takes over → reposition if needed.

---

## TASK 3: UI Layout Fixes (Portrait Home Screen)

### Reference
See the uploaded screenshot (IMG_2560.png) for current state.

### Bug Fix
1. **Good button not working:** Investigate the audio file path the Good button is trying to play. It's likely pointing to a wrong or missing file. Fix the path and verify the audio plays on tap.

### Layout Changes

2. **Remove "Hold to Talk" and "Tap to Listen" from their current position** (the two large circular buttons on the video feed area). "Tap to Listen" is removed entirely (replaced by audio streaming). "Hold to Talk" (PTT) moves to the action button row.

3. **Action button row (Good / Call Dog / Give Treat / Want Treat / No):**
   - Add PTT (mic icon) to this row. It should be a hold-to-record button, same functionality as before, just relocated.
   - This row now has 6 items: PTT / Good / Call Dog / Give Treat / Want Treat / No.
   - If 6 is too crowded, PTT can be a small floating mic icon near the action row instead.

4. **Lighting + Blue + Music player — merge into one compact row:**
   - Lighting button and Blue button side by side on the left (they're both light controls).
   - Music player controls compact on the right of the same row.
   - Volume slider below this row (or integrated).
   - Current state has BluLight floating far right and music player in the middle — consolidate.

5. **Remove the Drive / Missions / Settings card row entirely.**
   - Drive becomes a standalone prominent button on the home screen (above the bottom nav or floating).
   - Missions is in the bottom nav.
   - Settings is in the bottom nav.

6. **Bottom navigation bar:**
   - 6 items: **Home / Dogs / Missions / Photos / Activity / Settings**
   - Remove the duplicate Missions entry point (was in both card row and bottom nav).

7. **Portrait scaling:**
   - The video feed area should scale better to fill available space above the controls.
   - Controls below the video should be compact and not require scrolling on smaller screens.

### Landscape Layout Changes

8. **Remove "Hold to Talk" and "Tap to Listen" buttons** from landscape center area (same as portrait — PTT moves to a small icon accessible from landscape, listen is replaced by streaming).

9. **Add mode selector** to top-right area near emergency stop button:
   - Small pill showing current mode (e.g., "MANUAL").
   - Tappable to show: Manual / Coach / Mission options.
   - See Task 2 item 4 for behavior.

10. **"Switching to Manual Mode..." overlay:**
    - This should be a brief toast (1-2 seconds) that auto-dismisses, NOT a persistent blocking overlay.
    - Only show it during the actual mode transition. Once the robot confirms the mode change via `status_update`, dismiss immediately.

---

## TASK 4: Diagnostic Audit — Mode Switching Bugs

### Context
Morgan reported intermittent issues where:
- Selecting Manual from the dropdown would briefly switch to manual then immediately revert to idle.
- Pressing Drive would trigger "Switching to Manual Mode" but the robot would stay in idle.
- These issues are currently NOT manifesting but root cause is unknown.

### Audit Requirements

1. **Audit Drive button press flow:**
   - What exact sequence of commands is sent when Drive is pressed?
   - Is `set_mode manual` sent once or multiple times?
   - Is there any debounce or guard against rapid re-taps?
   - Is there a race condition between the mode dropdown and the Drive button?

2. **Audit mode state management:**
   - Where is the current mode stored in app state?
   - Is it updated optimistically (before robot confirms) or only on robot `status_update`?
   - Could the robot's `status_update` with "idle" arrive AFTER the app sends "manual", causing the UI to show idle even though manual was requested?
   - Is there a listener that reacts to `status_update` events and overwrites the mode the user just selected?

3. **Audit WebSocket reconnection:**
   - When the WebSocket reconnects, does the app re-send the current mode?
   - If the robot resets to idle on reconnect and sends a `status_update`, does the app blindly accept it?

4. **Audit login flow differences:**
   - Does the behavior differ between fresh login vs returning session?
   - Are there any stale state artifacts from a previous session that could interfere?

5. **Document findings:**
   - Create `MODE_AUDIT_FINDINGS.md` in the project root.
   - For each potential issue: describe the code path, whether it could cause the reported behavior, current status (fixed / unfixed / dormant).

---

## Files to Deliver
- Updated WebRTC handling to accept audio track
- Mute/unmute toggle widget
- Updated mode dropdown (portrait: idle/guardian/coach)
- Landscape mode selector (manual/coach/mission)
- Previous mode restore logic
- Updated set_mode command with source/timestamp
- Restructured portrait home screen layout
- Updated landscape layout
- Updated bottom navigation (6 items)
- Good button audio fix
- `MODE_AUDIT_FINDINGS.md`
