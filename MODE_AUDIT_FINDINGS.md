# MODE_AUDIT_FINDINGS.md

> **Relay Server Session Lifecycle & Stale Command Audit**
> Date: 2026-02-22 | Build 43 | Auditor: Claude (Relay)

---

## 1. Session Lifecycle Audit

### 1a. Robot Connection Replacement

**Code:** `app/connection_manager.py:57-77`

When a robot reconnects with the same `device_id`, `connect_robot()` checks if an existing connection exists (line 62) and calls `disconnect_robot()` on it first (line 64). This closes the old WebSocket and removes its metadata. The new connection then replaces it in `robot_connections`.

**Finding:** Clean. Old connection is fully terminated before new one is registered. No window for dual robot connections on the same device_id.

**Severity:** None — working correctly.

---

### 1b. App Connection Multi-Session

**Code:** `app/connection_manager.py:79-98`

Unlike robots, app connections are stored as a **list per user_id** (`app_connections[user_id] = [ws1, ws2, ...]`). Multiple app sessions for the same user are explicitly supported. When a new app connects, it's appended to the list — existing connections are NOT terminated.

**Finding:** This is intentional design (user could have multiple devices/tabs). However, this means if the app reconnects without the old connection being properly closed (e.g., network drop), **both connections coexist** until the old one is detected as dead via failed send or ping timeout.

**Could this cause mode-switching bugs?** Yes, in a narrow window:
- Old zombie connection is still in the list
- App sends `set_mode manual` on new connection → forwarded to robot ✓
- Robot responds with `status_update` → relay sends to ALL app connections (line 288-311)
- Send to zombie connection fails → `disconnect_app()` is called → triggers grace period logic
- Grace period sends `user_disconnected` to robot after 10 minutes (not immediate, so this is fine)

**But there's a subtler issue:** During the window where both connections exist, `send_to_user_apps()` iterates over all connections. If the old connection is slow/buffered (not yet dead), both receive events. The app would process duplicate events from the old connection. This is cosmetic, not causal for mode bugs.

**Severity:** LOW — The dual-connection window exists but doesn't cause mode-switching issues directly.

---

### 1c. On-Connect Handlers — Does the Relay Send Default Commands?

**Code:** `app/routers/websocket.py:677-691` (`/ws/app`), `app/routers/websocket.py:1045-1059` (`/ws`)

When an app connects, the relay sends to the robot:
```json
{"type": "user_connected", "user_id": "..."}
```

**The relay does NOT send any `set_mode` command on connection.** It does not send `set_mode idle` or any other mode command. The `user_connected` message is purely informational.

**However:** What the **robot** does when it receives `user_connected` is outside relay scope. If the robot interprets `user_connected` as "reset to idle mode," that would cause the reported bug pattern (set_mode manual → robot reverts to idle). **This is a robot-side concern, not a relay issue.**

When the robot connects (`/ws/device`), the relay sends `robot_connected` and `robot_status` events to the app. No mode commands are sent to the robot.

**When grace period expires** (`connection_manager.py:255-263`), the relay sends `user_disconnected` to the robot. Again, how the robot handles this is robot-side.

**Severity:** NONE from relay perspective — but the `user_connected`/`user_disconnected` messages should be investigated robot-side as potential triggers for mode resets.

**Recommendation:** Add the `user_connected` message to the robot audit checklist. Ask: "Does the robot reset mode to idle when it receives `user_connected`?"

---

## 2. Command Queue / Buffering Audit

**Code:** Entire `app/connection_manager.py` and `app/routers/websocket.py`

### Does the relay buffer or queue commands?

**NO.** There is no command queue, buffer, or backlog anywhere in the codebase. Confirmed by:
- Grepping for `queue`, `buffer`, `backlog`, `pending`, `replay` — zero matches
- Reading all of `ConnectionManager` — no list/deque/dict stores commands for later delivery
- `forward_command_to_robot()` (line 314-334) calls `send_to_robot()` immediately, which does `websocket.send_json()` synchronously

### What happens when the robot is offline?

If the robot is disconnected when a command arrives:
- `send_to_robot()` (line 272-286) checks `robot_connections.get(device_id)` — returns `None`
- Logs `"robot not connected"` warning
- Returns `False`
- App receives `DEVICE_OFFLINE` error response
- **Command is dropped. It is never stored for later delivery.**

### Could a stale command be delivered on reconnect?

**NO.** There is no mechanism to store or replay commands. When a robot reconnects, it gets a fresh `connect_robot()` call. The only messages sent to the robot on connect are:
- `user_connected` (if an app user is online)
- That's it.

**Severity:** NONE — no queuing means no stale command delivery from relay. This is clean.

---

## 3. Status Event Forwarding Audit

### 3a. Immediate Forwarding

**Code:** `app/routers/websocket.py:440-451` (`/ws/device`), lines 1126-1134 (`/ws`)

When the robot sends `status_update`, the relay calls `send_to_user_apps()` immediately within the same `receive_text()` iteration. There is no batching, debouncing, or delay.

**Could there be a race condition?** The relay is single-threaded per WebSocket connection (asyncio). Each WebSocket endpoint runs in its own coroutine. The sequence is:

1. App sends `set_mode manual` → relay receives on app coroutine → immediately forwards to robot via `send_to_robot()`
2. Robot processes mode change → sends `status_update` with new mode → relay receives on device coroutine → immediately forwards to app

These are on separate coroutines but share the same event loop. The ordering is:
- App command is forwarded to robot BEFORE the robot can process and respond
- Robot's status_update can only arrive AFTER it has processed the command
- Relay forwards the status_update as soon as it arrives

**There is no ordering inversion at the relay level.** The relay cannot send the robot's "idle" status before the app's "manual" command, because it simply forwards messages as they arrive.

**However:** If the robot sends a `status_update idle` (for an unrelated reason) close in time to the app's `set_mode manual`, the relay will forward both faithfully. The app would see: manual command sent → idle status received → confusion. This is a **robot timing issue**, not a relay issue.

### 3b. Status Caching

**No status cache exists.** Confirmed by grep — no `last_status`, `cached_status`, or similar. The relay is purely a pass-through for status events.

On app reconnect, the relay sends `robot_status` (online/offline boolean only, line 680-683). It does NOT send any cached mode state. The app has no way to get the robot's current mode from the relay — it must query the robot directly or wait for the next `status_update`.

**Severity:** NONE — relay forwarding is clean and immediate. No stale status delivery.

---

## 4. Single-Session-Per-Robot Enforcement

### 4a. Robot Side — Strict Single Session

**Code:** `app/connection_manager.py:57-77`

`connect_robot()` enforces one connection per `device_id`. If a robot reconnects, the old connection is forcibly closed. This is correct and clean.

### 4b. App Side — Multi-Session Allowed

**Code:** `app/connection_manager.py:79-98`

Multiple app sessions per user are allowed by design. When any app session sends a command, it's forwarded to the robot. There is no conflict resolution between sessions.

**Could two app sessions send conflicting commands?** Yes — if a user has two app instances open, both could send `set_mode` commands. The relay forwards all of them. This is by design (the user is the same person) but could cause apparent mode-switching flicker if both apps are fighting.

**Race condition during session handoff:** When an app disconnects and reconnects:
1. Old connection enters `finally` block → `disconnect_app()` removes it from list
2. If last connection → starts 10-minute grace period
3. New connection arrives → calls `cancel_grace_period()` → restores WebRTC sessions
4. Between step 1 and step 3, there's a window where no app connection exists for the user

During this window, robot events would be dropped (no app to forward to). Commands can't be sent (no app connected). This is expected behavior, not a bug.

**Severity:** LOW — multi-session is by design. The disconnect/reconnect window is expected.

---

## 5. WebSocket Ping/Pong and Timeout Audit

### 5a. Current Settings

**Code:** `app/config.py:19-23`, `run.py:12-13`

| Setting | Value | Source |
|---------|-------|--------|
| `ws_ping_interval` | 30s | `run.py` (env: `WS_PING_INTERVAL`) |
| `ws_ping_timeout` | 60s | `run.py` (env: `WS_PING_TIMEOUT`) |
| `ws_heartbeat_interval` | 30s | `config.py` (unused in practice — uvicorn handles pings) |
| `ws_connection_timeout` | 120s | `config.py` (unused in practice) |

Uvicorn sends WebSocket pings every 30 seconds. If no pong is received within 60 seconds, the connection is closed.

### 5b. Zombie Connection Lifetime

**Worst case:** A network silently drops. Uvicorn sends a ping. No pong comes back. After 60 seconds, uvicorn closes the connection. The `WebSocketDisconnect` exception fires, triggering cleanup.

**Maximum zombie lifetime: 90 seconds** (up to 30s before the next ping + 60s timeout).

During this 90-second window, the zombie connection is still in `robot_connections` or `app_connections`. If a new connection arrives for the same device_id during this window:

- **Robot:** `connect_robot()` calls `disconnect_robot()` on the old WebSocket, which attempts `websocket.close()`. This may fail silently (network is dead), but the old connection is removed from `robot_connections` and `connection_metadata`. **The new connection takes over cleanly.**

- **App:** The new connection is simply appended to the list. Both coexist until the zombie is detected dead (next failed send or ping timeout). During this window, events are sent to both, and the send to the zombie fails → `disconnect_app()` is called → cleanup happens.

**Could a zombie interfere with mode switching?** Only indirectly — if the robot sends a status_update during the zombie window, the send to the zombie fails, triggering cleanup. But the cleanup doesn't send any mode commands. The send to the live connection succeeds. **No mode interference.**

### 5c. Grace Period Interaction

When the last app connection disconnects, a 10-minute grace period starts. If a new connection arrives within 10 minutes, `cancel_grace_period()` restores WebRTC sessions but does NOT replay any commands or send any mode-related messages.

**After grace period expires** (10 minutes), the relay sends `user_disconnected` to the robot. This is the only "delayed" message the relay sends, and it contains no mode information.

**Severity:** NONE — ping/pong and timeout behavior is appropriate. Zombie window is bounded at 90s and doesn't cause mode interference.

---

## 6. Summary

| Audit Item | Status | Could Cause Mode Bug? | Severity |
|------------|--------|----------------------|----------|
| Robot connection replacement | Clean | No | NONE |
| App multi-session coexistence | By design | Unlikely (same user) | LOW |
| On-connect default commands | None sent by relay | No (check robot-side) | NONE |
| Command queue/buffering | None exists | No | NONE |
| Stale command delivery | Impossible (no queue) | No | NONE |
| Status event forwarding | Immediate, no cache | No | NONE |
| Single-session enforcement (robot) | Correct | No | NONE |
| Ping/pong zombie window | 90s max, bounded | No | NONE |
| Grace period behavior | Clean | No | NONE |

### Root Cause Assessment

**The relay is NOT the cause of the reported mode-switching bugs.** The relay is a stateless pass-through for commands and events. It does not:
- Queue or replay commands
- Cache or modify status events
- Send any mode-related commands on connect/disconnect
- Introduce ordering inversions

**Most likely root causes to investigate (robot-side and app-side):**
1. **Robot reacting to `user_connected` by resetting mode** — if the robot treats `user_connected` as "new session, reset to idle," this explains the pattern perfectly.
2. **App sending duplicate `set_mode` commands** — e.g., sending `set_mode manual` followed immediately by `set_mode idle` during a UI transition race.
3. **Robot mode change processing order** — if the robot's mode change handler is async and a second `set_mode` arrives before the first completes.

**Recommended next steps:**
- Audit robot's `user_connected` handler — does it reset mode?
- Add the Task 1 `set_mode` enhanced logging to the relay (being implemented alongside this audit) to capture the exact sequence of mode commands and their sources for the next occurrence.
