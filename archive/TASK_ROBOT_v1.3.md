# ROBOT CLAUDE — Task Batch v1.3

> Coordinated update. Read API_CONTRACT_v1_3.md first — it is the source of truth.
> This batch covers: always-on audio streaming, mode system updates, and diagnostic audit.

---

## TASK 1: Always-On WebRTC Audio Track

### What
Add the USB microphone as a persistent audio track on the WebRTC PeerConnection, alongside the existing video track. The robot should always offer both video + audio in its SDP offer.

### Requirements

1. **Add audio track to PeerConnection:**
   - Capture USB mic input (16kHz mono, same hardware you already use for audio clips).
   - Create a WebRTC audio track from the mic source.
   - Add this track to the PeerConnection BEFORE creating the SDP offer.
   - The SDP offer must contain both video and audio media lines.
   - Codec: Opus (WebRTC default). Let the WebRTC stack handle encoding — do not manually encode to AAC.

2. **Mode-aware mic muting:**
   - In `idle` and `manual` modes: Mic feeds audio frames to the WebRTC audio track normally.
   - In `silent_guardian`, `coach`, and `mission` modes: Mute the audio track source. Do NOT remove the track or renegotiate SDP. Just stop feeding mic frames / set track.enabled = false (or equivalent in your WebRTC library).
   - On every `set_mode` command, check the new mode and mute/unmute accordingly.
   - This must be instant — no perceptible delay on mode switch.

3. **Echo suppression during PTT playback:**
   - When the robot receives an `audio_message` (PTT from app) and plays it through the speaker, temporarily mute the outbound WebRTC audio track for the duration of playback.
   - Resume the audio track after playback completes.
   - This prevents the robot's mic from picking up its own speaker output and echoing it back to the app.

4. **Adaptive quality (stretch goal):**
   - Monitor WebRTC connection stats (packet loss, round-trip time).
   - If packet loss > 5% or RTT > 500ms, reduce audio bitrate first (before touching video).
   - If severe congestion (packet loss > 15%), mute audio track temporarily to preserve video bandwidth.
   - Log all quality adaptation events.

### What NOT to change
- PTT (app→robot) stays on WebSocket. Do not touch that path.
- Do not remove the `audio_message` handler for receiving PTT from the app.
- Do not change video track behavior.

### Testing
- Connect from app, verify SDP offer contains both `m=video` and `m=audio` lines.
- In idle/manual: verify audio is being transmitted (check WebRTC stats for audio bytes sent > 0).
- Switch to coach mode: verify audio bytes sent drops to ~0 (silence).
- Switch back to idle: verify audio resumes.
- Trigger PTT while in idle: verify robot speaker plays the message, and outbound audio track briefly mutes then resumes.

---

## TASK 2: Mode System Updates

### What
Update `set_mode` handling to accept the new `source` and `timestamp` fields, and enforce resolution constraints.

### Requirements

1. **Accept new set_mode format:**
   ```json
   {
     "type": "command",
     "command": "set_mode",
     "data": {
       "mode": "manual",
       "source": "drive_enter",
       "timestamp": "2026-02-22T10:00:00Z"
     }
   }
   ```
   - `source` values: `dropdown`, `drive_enter`, `drive_exit_restore`, `mission_start`, `mission_end`, `landscape_selector`
   - `timestamp` is ISO8601 from the app.
   - Both fields are optional for backward compatibility. If missing, default source to `"unknown"` and timestamp to current time.

2. **Log every mode change with full context:**
   - Log: previous mode, new mode, source, app timestamp, server timestamp.
   - Format: `MODE_CHANGE: {prev} -> {new} | source={source} | app_ts={timestamp} | server_ts={now}`
   - This is critical for diagnosing the intermittent idle/manual bugs.

3. **Resolution switching on mode change:**
   - `idle`: Standard resolution (current default).
   - `manual`: High resolution (up to 4K or whatever the current max is). No AI processing.
   - `silent_guardian` / `coach` / `mission`: Switch to 640x640. Start AI/Hailo processing.
   - Ensure the camera pipeline switches cleanly without crashing. If the resolution change requires restarting the camera, do it gracefully.

4. **Audio track mute/unmute on mode change** (see Task 1).

### What NOT to change
- The robot does not decide which modes are valid from which context. The app handles that logic. The robot just executes whatever mode it receives.

---

## TASK 3: Diagnostic Audit — Mode Switching Bugs

### Context
Morgan reported intermittent issues where:
- Selecting Manual from the dropdown would briefly switch to manual then immediately revert to idle.
- Pressing Drive would show "Switching to Manual Mode" but the robot would announce "status idle" and stay in idle.
- These issues were intermittent — sometimes reproducible, sometimes not. Currently not manifesting but root cause unknown.

### Audit Requirements

1. **Search codebase for any automatic revert-to-idle behavior:**
   - Is there a watchdog timer that resets to idle after inactivity?
   - Is there a startup/initialization sequence that forces idle?
   - Is there any health check that resets mode?
   - Is there a "default mode" that gets set on WebSocket connect/reconnect?

2. **Search for race conditions in mode setting:**
   - If two `set_mode` commands arrive in quick succession, does the second always win?
   - Is there any async processing that could cause an older command to execute after a newer one?
   - Is mode state atomic or could it be partially updated?

3. **Check WebSocket reconnection behavior:**
   - When the WebSocket reconnects, does the robot reset to idle?
   - Does the relay re-send any cached state that could override a recent mode change?

4. **Document findings:**
   - Create a file `MODE_AUDIT_FINDINGS.md` in the project root.
   - For each potential issue found: describe the code path, whether it could cause the reported behavior, and whether it's been fixed or still exists.
   - If the issue was fixed, document WHEN and HOW (commit reference if possible).
   - If the issue was NOT fixed but is not manifesting, explain why it might be dormant and what could trigger it again.

---

## Files to Deliver
- Updated WebRTC setup code with audio track
- Updated mode handler with source/timestamp logging
- Updated mic mute logic per mode
- `MODE_AUDIT_FINDINGS.md` with diagnostic results
