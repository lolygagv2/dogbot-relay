# RELAY SERVER CLAUDE — Task Batch v1.3

> Coordinated update. Read API_CONTRACT_v1_3.md first — it is the source of truth.
> This batch is lighter than Robot/App — audio streaming is P2P via WebRTC and doesn't touch the relay.
> Focus: command audit, session lifecycle, and diagnostic investigation.

---

## TASK 1: set_mode Command — Pass Through Source and Timestamp

### What
The `set_mode` command now includes `source` and `timestamp` fields. The relay must pass these through and log them.

### Requirements

1. **Pass through the full data payload:**
   - The relay already forwards commands from app to robot. Ensure `source` and `timestamp` fields in the `set_mode` data are not stripped or modified.
   - No schema validation needed on these fields — just pass them through.

2. **Enhanced logging for set_mode commands:**
   - Log every `set_mode` command that passes through the relay with:
     - App user/session ID
     - Target device_id
     - Mode value
     - Source value
     - App timestamp
     - Relay server timestamp
     - Client IP
   - Format: `SET_MODE: user={user} device={device_id} mode={mode} source={source} app_ts={timestamp} relay_ts={now} ip={client_ip}`
   - This logging is critical for diagnosing mode switching bugs.

---

## TASK 2: Diagnostic Audit — Session Lifecycle and Stale Commands

### Context
Morgan reported intermittent issues where the robot would switch to manual then immediately revert to idle, or Drive mode would fail to stick. These issues are currently not manifesting but root cause is unknown. The relay server is a prime suspect for stale command delivery or session conflicts.

### Audit Requirements

1. **Session lifecycle audit:**
   - When a new WebSocket connection is established for a user+device pair, is the old connection fully terminated?
   - Is there ANY window where two active connections could exist for the same device?
   - When a connection drops and reconnects, is there any queued command backlog that gets replayed?
   - Are there any "on connect" or "on reconnect" handlers that send default commands (like set_mode idle)?

2. **Command queue / buffering audit:**
   - Does the relay buffer or queue commands when the robot is temporarily disconnected?
   - If so, are queued commands timestamped? Is there an expiry?
   - Could a command from minutes/hours/days ago be delivered when the robot reconnects?
   - If no queue exists, confirm this explicitly.

3. **Status event forwarding audit:**
   - When the robot sends a `status_update` event, does the relay forward it immediately to the app?
   - Could there be a race condition where the robot's "idle" status_update arrives at the relay AFTER the app's "set_mode manual" command, but gets forwarded to the app first?
   - Is there any caching of the last known status that could send stale state to a reconnecting app?

4. **Single-session enforcement verification:**
   - Verify the single-session-per-robot enforcement is working correctly.
   - Test: Connect two app sessions to the same device_id. Confirm the first is terminated cleanly.
   - Check: Is there a race condition during session handoff where both sessions could send commands?

5. **WebSocket ping/pong and timeout audit:**
   - What are the current ping/pong intervals and connection timeout values?
   - Could a "zombie" connection (network silently dropped) persist long enough to interfere with a new connection?
   - Is there a keepalive mechanism that detects dead connections proactively?

### Document Findings

6. **Create `MODE_AUDIT_FINDINGS.md` in the relay project root:**
   - For each audit item above: describe what you found, whether it could cause the reported mode-switching bugs, and current status.
   - Include code references (file and line numbers) for each finding.
   - If you find an unfixed issue, flag it clearly with severity and recommended fix.

---

## TASK 3: PTT Audio Passthrough — Verify No Changes Needed

### What
Confirm that push-to-talk audio (app→relay→robot via WebSocket) continues to work unchanged. The always-on audio streaming is P2P WebRTC and does not involve the relay.

### Requirements
1. Verify `audio_message` type messages (app→robot direction) are still forwarded correctly.
2. Verify `audio_played` type messages (robot→app direction) are still forwarded correctly.
3. No code changes should be needed here — just verify and confirm.

---

## Files to Deliver
- Updated logging for set_mode commands (if not already sufficient)
- `MODE_AUDIT_FINDINGS.md` with session lifecycle and stale command audit results
- Confirmation that PTT passthrough is unaffected
