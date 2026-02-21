import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.auth import decode_token, verify_device_signature, verify_device_signature_with_timestamp
from app.config import Settings, get_settings
from app.connection_manager import ConnectionManager, get_connection_manager
from app.database import (
    get_device_owner as db_get_device_owner,
    get_metrics as db_get_metrics,
    get_user_dogs,
    log_metric as db_log_metric,
    log_mission as db_log_mission,
)
from app.routers.device import get_device_data, update_device_online_status
from app.services.turn_service import turn_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# Track active WebRTC sessions
# session_id -> {"app_ws": WebSocket, "device_id": str, "user_id": str}
webrtc_sessions: dict[str, dict] = {}

# Stale command threshold in milliseconds
STALE_COMMAND_THRESHOLD_MS = 2000

# Large message rejection threshold (1MB) - Build 38
# Messages over this size should use HTTP upload instead
MAX_WEBSOCKET_MESSAGE_SIZE = 1 * 1024 * 1024

# Large message threshold for logging warnings (5MB)
LARGE_MESSAGE_THRESHOLD = 5 * 1024 * 1024

# Event types that should be logged for verification (P2: Build 34)
# Extended in Build 34 Task 2 & 3 to include upload and mission completion events
TRACKED_EVENTS = {
    # Mission events
    "mission_progress",
    "mission_complete",
    "mission_stopped",
    # Mode/detection events
    "mode_changed",
    "dog_detected",
    "treat_dispensed",
    # Upload events (Robot -> App)
    "upload_complete",
    "upload_error",
    "upload_result",
    # Audio state
    "audio_state",
    # Schedule events (Robot -> App) - Build 38
    "schedule_created",
    "schedule_updated",
    "schedule_deleted",
    "schedule_triggered",
}


def get_client_ip(websocket: WebSocket) -> str:
    """Extract client IP from WebSocket, checking X-Forwarded-For for proxied connections."""
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        # X-Forwarded-For can be comma-separated; first entry is the real client
        return forwarded_for.split(",")[0].strip()
    if websocket.client:
        return websocket.client.host
    return "unknown"


def is_stale_command(message: dict) -> tuple[bool, int]:
    """
    Check if a command is stale based on its timestamp.
    Returns (is_stale, age_ms).
    Commands without timestamps are not considered stale.
    """
    ts = message.get("timestamp")
    if ts is None:
        return False, 0

    now_ms = int(time.time() * 1000)
    age_ms = now_ms - ts

    return age_ms > STALE_COMMAND_THRESHOLD_MS, age_ms


# ============== WebRTC Signaling Handlers ==============

async def handle_webrtc_request(
    websocket: WebSocket,
    message: dict,
    user_id: str,
    manager: ConnectionManager
):
    """App requests video stream from robot."""
    device_id = message.get("device_id")

    if not device_id:
        # Default to first owned device
        user_devices = manager.get_user_devices(user_id)
        if user_devices:
            device_id = user_devices[0]

    if not device_id:
        await websocket.send_json({
            "type": "error",
            "code": "NO_DEVICE",
            "message": "No device specified and no devices paired"
        })
        return

    # Verify user owns this device
    owner = manager.get_device_owner(device_id)
    if owner != user_id:
        await websocket.send_json({
            "type": "error",
            "code": "NOT_AUTHORIZED",
            "message": "Not authorized to access this device"
        })
        return

    # Check if robot is online
    if not manager.is_robot_online(device_id):
        await websocket.send_json({
            "type": "error",
            "code": "DEVICE_OFFLINE",
            "message": "Device is offline"
        })
        return

    # Close any existing session for this device before creating a new one
    # This returns a new session_id and notifies the robot to close the old one
    session_id = await manager.create_webrtc_session(device_id, user_id)

    # Clean old sessions for this device from routing table
    old_session_ids = [
        sid for sid, s in webrtc_sessions.items()
        if s.get("device_id") == device_id
    ]
    for old_sid in old_session_ids:
        webrtc_sessions.pop(old_sid, None)
        logger.info(f"[WEBRTC] Removed stale routing entry {old_sid} for device {device_id}")

    # Generate TURN credentials with 24-hour TTL
    settings = get_settings()
    try:
        ice_servers = await turn_service.generate_credentials(ttl=settings.turn_credential_ttl)
        logger.info(f"[TURN] Generated credentials for WebRTC session {session_id}, TTL={settings.turn_credential_ttl}s")
    except Exception as e:
        logger.error(f"TURN credential generation failed: {e}")
        # Roll back session tracking on failure
        await manager.close_webrtc_session(session_id, device_id)
        await websocket.send_json({
            "type": "error",
            "code": "TURN_ERROR",
            "message": f"Failed to generate TURN credentials: {str(e)}"
        })
        return

    # Track session in routing table
    webrtc_sessions[session_id] = {
        "app_ws": websocket,
        "device_id": device_id,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc)
    }

    # Send credentials to app
    await websocket.send_json({
        "type": "webrtc_credentials",
        "session_id": session_id,
        "ice_servers": ice_servers.get("iceServers", ice_servers)
    })

    # Forward request to robot with credentials
    await manager.send_to_robot(device_id, {
        "type": "webrtc_request",
        "session_id": session_id,
        "ice_servers": ice_servers.get("iceServers", ice_servers)
    })

    logger.info(f"[WEBRTC] Session {session_id} initiated: user {user_id} -> device {device_id}")
    logger.info(f"[WEBRTC] Routing table entries: {len(webrtc_sessions)}, Manager active sessions: {manager.webrtc_sessions}")


async def handle_webrtc_offer(message: dict, device_id: str, manager: ConnectionManager):
    """Forward WebRTC offer from robot to app."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)

    logger.info(f"[WEBRTC] Offer from device {device_id}, session {session_id}, active device session: {manager.webrtc_sessions.get(device_id)}")

    if session and session["app_ws"] and session["device_id"] == device_id:
        try:
            await session["app_ws"].send_json(message)
            logger.info(f"[WEBRTC] Forwarded offer for session {session_id}")
        except Exception as e:
            logger.error(f"[WEBRTC] Failed to forward offer for session {session_id}: {e}")
    else:
        logger.warning(f"[WEBRTC] Ignoring offer for unknown/stale session {session_id}")


async def handle_webrtc_answer(message: dict, user_id: str, manager: ConnectionManager):
    """Forward WebRTC answer from app to robot."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)

    logger.info(f"[WEBRTC] Answer from user {user_id}, session {session_id}")

    if session and session["user_id"] == user_id:
        device_id = session["device_id"]
        success = await manager.send_to_robot(device_id, message)
        if success:
            logger.info(f"[WEBRTC] Forwarded answer for session {session_id} to device {device_id}")
        else:
            logger.error(f"[WEBRTC] Failed to forward answer for session {session_id}")
    else:
        logger.warning(f"[WEBRTC] Ignoring answer for unknown/stale session {session_id}")


async def handle_webrtc_ice(
    message: dict,
    from_type: str,
    identifier: str,
    manager: ConnectionManager
):
    """Forward ICE candidate to the other peer."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)

    if not session:
        logger.debug(f"[WEBRTC] ICE candidate for unknown session {session_id} from {from_type} {identifier}")
        return

    if from_type == "robot":
        # From robot, forward to app
        if session["device_id"] == identifier and session["app_ws"]:
            try:
                await session["app_ws"].send_json(message)
            except Exception as e:
                logger.error(f"[WEBRTC] Failed to forward ICE to app: {e}")
    else:
        # From app, forward to robot
        if session["user_id"] == identifier:
            device_id = session["device_id"]
            await manager.send_to_robot(device_id, message)


async def handle_webrtc_close(message: dict, manager: ConnectionManager):
    """Clean up WebRTC session. Only notifies robot if this is the active session for the device."""
    session_id = message.get("session_id")
    session = webrtc_sessions.pop(session_id, None)

    if session:
        device_id = session["device_id"]
        active_session = manager.webrtc_sessions.get(device_id)
        is_active = active_session == session_id

        logger.info(f"[WEBRTC] Close requested for session {session_id} (device {device_id}), active={is_active}, current active={active_session}")

        # Notify app
        if session["app_ws"]:
            try:
                await session["app_ws"].send_json({"type": "webrtc_close", "session_id": session_id})
            except Exception:
                pass

        # Only notify robot and clean up manager tracking if this is the active session
        # Stale session closes should NOT trigger robot mode revert
        if is_active:
            await manager.close_webrtc_session(session_id, device_id)
            logger.info(f"[WEBRTC] Active session {session_id} closed for device {device_id}")
        else:
            logger.info(f"[WEBRTC] Stale session {session_id} cleaned from routing table (device {device_id} active session unaffected)")
    else:
        logger.warning(f"[WEBRTC] Close requested for unknown session {session_id}")


def cleanup_sessions_for_websocket(websocket: WebSocket, manager: ConnectionManager = None):
    """Remove all WebRTC sessions associated with a websocket."""
    sessions_to_remove = []
    for session_id, session in webrtc_sessions.items():
        if session.get("app_ws") == websocket:
            sessions_to_remove.append((session_id, session.get("device_id")))

    for session_id, device_id in sessions_to_remove:
        webrtc_sessions.pop(session_id, None)
        # Also clean manager tracking if this was the active session
        if manager and device_id and manager.webrtc_sessions.get(device_id) == session_id:
            del manager.webrtc_sessions[device_id]
            logger.info(f"[WEBRTC] Removed active session {session_id} for device {device_id} (app disconnect)")
        else:
            logger.info(f"[WEBRTC] Removed routing entry {session_id} (app disconnect)")

    if manager:
        logger.info(f"[WEBRTC] Active sessions after app cleanup: {manager.webrtc_sessions}")


def cleanup_sessions_for_device(device_id: str, manager: ConnectionManager = None):
    """Remove all WebRTC sessions associated with a device."""
    sessions_to_remove = []
    for session_id, session in webrtc_sessions.items():
        if session.get("device_id") == device_id:
            sessions_to_remove.append(session_id)

    for session_id in sessions_to_remove:
        webrtc_sessions.pop(session_id, None)
        logger.info(f"[WEBRTC] Removed routing entry {session_id} (device {device_id} disconnect)")

    # Also clean manager tracking
    if manager and device_id in manager.webrtc_sessions:
        removed_session = manager.webrtc_sessions.pop(device_id)
        logger.info(f"[WEBRTC] Removed active session {removed_session} for device {device_id} (device disconnect)")

    if manager:
        logger.info(f"[WEBRTC] Active sessions after device cleanup: {manager.webrtc_sessions}")


# ============== WebSocket Endpoints ==============

@router.websocket("/ws/device")
async def websocket_device_endpoint(
    websocket: WebSocket,
    device_id: str = Query(None),
    sig: str = Query(None),
    timestamp: str = Query(None),
    settings: Settings = Depends(get_settings),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    WebSocket endpoint for robot devices.
    Robots connect with their device_id and HMAC signature.

    Supports two auth methods:
    1. Query params: /ws/device?device_id=xxx&sig=xxx&timestamp=xxx
    2. Headers: X-Device-ID, X-Signature, X-Timestamp

    Signature is HMAC-SHA256(device_id + timestamp, device_secret)
    """
    # Try to get credentials from headers if not in query params
    if not device_id:
        device_id = websocket.headers.get("x-device-id")
    if not sig:
        sig = websocket.headers.get("x-signature")
    if not timestamp:
        timestamp = websocket.headers.get("x-timestamp")

    # Validate we have required params
    if not device_id or not sig:
        logger.warning(f"Missing device_id or sig. device_id={device_id}, sig={bool(sig)}")
        await websocket.close(code=4000, reason="Missing device_id or sig parameter")
        return

    # Log what we received for debugging
    logger.info(f"Device auth attempt: device_id={device_id}, timestamp={timestamp}, sig={sig[:16]}...")

    # Verify device signature - try with timestamp first, then without
    sig_valid = verify_device_signature_with_timestamp(device_id, timestamp, sig, settings.device_secret)

    if not sig_valid:
        logger.warning(f"Signature verification failed for {device_id}")
        await websocket.close(code=4001, reason="Invalid device signature")
        return

    logger.info(f"Device {device_id} authenticated successfully")

    # Get owner from database (authoritative source for pairings)
    owner_id = db_get_device_owner(device_id)
    if owner_id:
        logger.info(f"Device {device_id} paired to user {owner_id} (from DB)")
    else:
        logger.warning(f"Device {device_id} has no owner in database")

    # Extract client IP
    client_ip = get_client_ip(websocket)

    # Connect the robot
    await manager.connect_robot(websocket, device_id, owner_id, ip=client_ip)
    update_device_online_status(device_id, True)

    # Broadcast robot_connected event to owner's apps
    owner_id = manager.get_device_owner(device_id)
    if owner_id:
        await manager.send_to_user_apps(owner_id, {
            "type": "event",
            "event": "robot_connected",
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await manager.send_to_user_apps(owner_id, {
            "type": "robot_status",
            "device_id": device_id,
            "online": True
        })
        logger.info(f"Broadcast robot_connected and robot_status online=true for {device_id} to user {owner_id}")

    try:
        while True:
            # Receive message from robot
            data = await websocket.receive_text()

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from robot {device_id}: {data}")
                continue

            msg_type = message.get("type")

            # Log large payloads with connection health monitoring (P1: Build 34)
            msg_size = len(data)
            if msg_size > LARGE_MESSAGE_THRESHOLD:
                logger.warning(f"[LARGE-MSG] Robot({device_id}): {msg_type}, {msg_size//1024//1024}MB - may affect connection stability")
            elif msg_size > 10000:
                logger.info(f"[LARGE] Robot({device_id}): {msg_type}, ~{msg_size//1000}KB")

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle WebRTC signaling from robot
            if msg_type == "webrtc_offer":
                await handle_webrtc_offer(message, device_id, manager)
                continue

            if msg_type == "webrtc_ice":
                await handle_webrtc_ice(message, "robot", device_id, manager)
                continue

            if msg_type == "webrtc_close":
                await handle_webrtc_close(message, manager)
                continue

            # Handle status_update from robot
            if msg_type == "status_update":
                # Ensure device_id is set
                if "device_id" not in message:
                    message["device_id"] = device_id

                owner_id = manager.get_device_owner(device_id)
                if owner_id:
                    await manager.send_to_user_apps(owner_id, message)
                    logger.info(f"[ROUTE] Robot({device_id}) -> App({owner_id}): status_update")
                else:
                    logger.warning(f"[ROUTE] Robot({device_id}) -> ???: status_update (no owner)")
                continue

            # Handle upload result events from robot (Build 34 Task 2)
            if msg_type in ("upload_complete", "upload_error", "upload_result"):
                if "device_id" not in message:
                    message["device_id"] = device_id
                if "timestamp" not in message:
                    message["timestamp"] = datetime.now(timezone.utc).isoformat()

                owner_id = manager.get_device_owner(device_id)
                filename = message.get("filename", "unknown")
                success = message.get("success", False)
                error = message.get("error")

                if success:
                    logger.info(f"[UPLOAD] Success from {device_id}: {filename}")
                else:
                    logger.warning(f"[UPLOAD] Failed from {device_id}: {filename} - {error}")

                if owner_id:
                    delivered = await manager.send_to_user_apps(owner_id, message)
                    if delivered > 0:
                        logger.info(f"[EVENT-OK] {msg_type} delivered to {delivered} app(s) for user {owner_id}")
                    else:
                        logger.warning(f"[EVENT-FAIL] {msg_type} NOT delivered - user {owner_id} has no connected apps")
                else:
                    logger.warning(f"[ROUTE] Robot({device_id}) -> ???: {msg_type} (no owner)")
                continue

            # Handle audio_state events from robot (Build 34 Task 3)
            if msg_type == "audio_state":
                if "device_id" not in message:
                    message["device_id"] = device_id

                owner_id = manager.get_device_owner(device_id)
                state = message.get("state", "unknown")
                logger.info(f"[AUDIO] State from {device_id}: {state}")

                if owner_id:
                    delivered = await manager.send_to_user_apps(owner_id, message)
                    if delivered > 0:
                        logger.info(f"[EVENT-OK] audio_state delivered to {delivered} app(s)")
                    else:
                        logger.warning(f"[EVENT-FAIL] audio_state NOT delivered - no apps connected")
                continue

            # Handle schedule events from robot (Build 41 - Schedule Event Verification)
            if msg_type in ("schedule_created", "schedule_updated", "schedule_deleted"):
                if "device_id" not in message:
                    message["device_id"] = device_id
                if "timestamp" not in message:
                    message["timestamp"] = datetime.now(timezone.utc).isoformat()

                owner_id = manager.get_device_owner(device_id)
                schedule_id = message.get("schedule_id", message.get("data", {}).get("schedule_id", "unknown"))

                logger.info(f"[SCHEDULE] {msg_type} from {device_id}: schedule_id={schedule_id}")

                if owner_id:
                    delivered = await manager.send_to_user_apps(owner_id, message)
                    if delivered > 0:
                        logger.info(f"[SCHEDULE-OK] {msg_type} delivered to {delivered} app(s)")
                    else:
                        logger.warning(f"[SCHEDULE-FAIL] {msg_type} NOT delivered - no apps connected")
                else:
                    logger.warning(f"[SCHEDULE] {msg_type} from {device_id} - no owner found")
                continue

            # Forward events to owner's apps (legacy "event" field format)
            if "event" in message:
                # Add timestamp if not present
                if "timestamp" not in message:
                    message["timestamp"] = datetime.now(timezone.utc).isoformat()

                # Add device_id to help apps identify source
                if "device_id" not in message:
                    message["device_id"] = device_id

                event_name = message.get("event")
                owner_id = manager.get_device_owner(device_id)

                # Enhanced logging for mission events
                if event_name == "mission_progress":
                    data = message.get("data", {})
                    logger.info(f"[MISSION] Progress event from {device_id}: status={data.get('status')} "
                                f"stage={data.get('stage')}/{data.get('total_stages')} "
                                f"mission_type={data.get('mission_type')}")
                    # Build 40: Warn if mission data is empty (diagnostic for debugging)
                    if not data.get("status") and not data.get("action"):
                        logger.warning(
                            f"[MISSION] Empty progress data from {device_id} — "
                            f"fields: status={data.get('status')}, action={data.get('action')}, "
                            f"stage={data.get('stage_number')}/{data.get('total_stages')}"
                        )
                elif event_name in ("mode_changed", "bark_detected", "dog_detected", "treat_dispensed"):
                    logger.info(f"[EVENT] {event_name} from {device_id}: {message.get('data', {})}")

                # Forward event and verify delivery (P2: Build 34 - Event Forwarding Verification)
                delivered_count = await manager.forward_event_to_owner(device_id, message)
                if event_name in TRACKED_EVENTS:
                    if delivered_count > 0:
                        logger.info(f"[EVENT-OK] {event_name} delivered to {delivered_count} app(s) for user {owner_id}")
                    else:
                        logger.warning(f"[EVENT-FAIL] {event_name} NOT delivered - user {owner_id} has no connected apps")
                else:
                    logger.info(f"[ROUTE] Robot({device_id}) -> App({owner_id}): event={event_name}")
                continue

            # Handle metric_event from robot
            if msg_type == "metric_event":
                owner_id = manager.get_device_owner(device_id)
                if owner_id:
                    dog_id = message.get("dog_id")
                    metric_type = message.get("metric_type")
                    value = message.get("value", 1)
                    mission_type = message.get("mission_type")
                    mission_result = message.get("mission_result")
                    details = message.get("details")

                    if dog_id and mission_type and mission_result:
                        db_log_mission(dog_id, owner_id, mission_type, mission_result, details)
                    elif dog_id and metric_type:
                        try:
                            db_log_metric(dog_id, owner_id, metric_type, value)
                        except ValueError as e:
                            logger.warning(f"[METRICS] Invalid metric from robot {device_id}: {e}")
                            continue

                    # Forward to owner's apps
                    message["device_id"] = device_id
                    await manager.send_to_user_apps(owner_id, message)
                    logger.info(f"[ROUTE] Robot({device_id}) -> App({owner_id}): metric_event")
                continue

            # Catch-all: forward any other type-based message to owner's apps
            if msg_type:
                if "device_id" not in message:
                    message["device_id"] = device_id

                owner_id = manager.get_device_owner(device_id)
                if owner_id:
                    await manager.send_to_user_apps(owner_id, message)
                    logger.info(f"[ROUTE] Robot({device_id}) -> App({owner_id}): {msg_type}")
                else:
                    logger.warning(f"[ROUTE] Robot({device_id}) -> ???: {msg_type} (no owner)")

    except WebSocketDisconnect:
        logger.info(f"Robot {device_id} disconnected")
    except Exception as e:
        logger.error(f"Error in robot websocket {device_id}: {e}")
    finally:
        # Clean up WebRTC sessions for this device
        cleanup_sessions_for_device(device_id, manager)

        # Get owner before disconnect clears state
        owner_id = manager.get_device_owner(device_id)

        await manager.disconnect_robot(websocket)
        update_device_online_status(device_id, False)

        # Broadcast robot_disconnected event and robot_status offline to owner's apps
        if owner_id:
            await manager.send_to_user_apps(owner_id, {
                "type": "event",
                "event": "robot_disconnected",
                "device_id": device_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            await manager.send_to_user_apps(owner_id, {
                "type": "robot_status",
                "device_id": device_id,
                "online": False
            })
            logger.info(f"Broadcast robot_disconnected and robot_status online=false for {device_id} to user {owner_id}")


@router.websocket("/ws/app")
async def websocket_app_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    settings: Settings = Depends(get_settings),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    WebSocket endpoint for mobile app clients.
    Apps connect with their JWT token.
    """
    # Decode and verify JWT token
    payload = decode_token(token, settings)
    if not payload:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token payload")
        return

    # Extract client IP
    client_ip = get_client_ip(websocket)

    # Connect the app
    await manager.connect_app(websocket, user_id, ip=client_ip)

    # Check for grace period reconnection
    restored_sessions = manager.cancel_grace_period(user_id)
    if restored_sessions:
        # Restore WebRTC sessions: update app_ws reference to new websocket
        for session_id, device_id in restored_sessions:
            if session_id in webrtc_sessions:
                webrtc_sessions[session_id]["app_ws"] = websocket
                logger.info(f"[GRACE] Restored WebRTC session {session_id} for device {device_id}")
                await websocket.send_json({
                    "type": "session_restored",
                    "session_id": session_id,
                    "device_id": device_id,
                })
        logger.info(f"[GRACE] User {user_id} reconnected, restored {len(restored_sessions)} session(s)")

    # Send connection acknowledgment
    await websocket.send_json({
        "type": "auth_result",
        "success": True,
        "user_id": user_id
    })

    # Send current status of user's paired devices and notify robots that user is online
    user_devices = manager.get_user_devices(user_id)
    for device_id in user_devices:
        await websocket.send_json({
            "type": "robot_status",
            "device_id": device_id,
            "online": manager.is_robot_online(device_id)
        })
        # Notify robot that user has connected (if robot is online)
        if manager.is_robot_online(device_id):
            await manager.send_to_robot(device_id, {
                "type": "user_connected",
                "user_id": user_id
            })
            logger.info(f"[CONNECT] Sent user_connected to robot {device_id} for user {user_id} from {client_ip}")

    # Send today's metrics for each of the user's dogs
    try:
        user_dogs = get_user_dogs(user_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for dog in user_dogs:
            dog_metrics = db_get_metrics(dog["id"], user_id, today)
            dog_metrics.pop("dog_id", None)
            await websocket.send_json({
                "type": "metrics_sync",
                "dog_id": dog["id"],
                "metrics": dog_metrics,
            })
    except Exception as e:
        logger.error(f"[METRICS] Failed to send metrics_sync to user {user_id}: {e}")

    try:
        while True:
            # Receive message from app
            data = await websocket.receive_text()

            # Track activity for grace period
            manager.update_activity(user_id)

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from app user {user_id}: {data}")
                continue

            msg_type = message.get("type")

            # Log large payloads with connection health monitoring (P1: Build 34)
            msg_size = len(data)

            # Reject messages over 1MB - use HTTP upload instead (Build 38)
            if msg_size > MAX_WEBSOCKET_MESSAGE_SIZE:
                logger.warning(f"[REJECTED] Message too large from App({user_id}): {msg_size//1024}KB, type={msg_type or message.get('command')}")
                await websocket.send_json({
                    "type": "error",
                    "code": "MESSAGE_TOO_LARGE",
                    "message": f"Message too large ({msg_size//1024}KB). Use HTTP upload for files over 1MB. See POST /api/music/upload"
                })
                continue

            if msg_size > LARGE_MESSAGE_THRESHOLD:
                logger.warning(f"[LARGE-MSG] App({user_id}): {msg_type or message.get('command')}, {msg_size//1024//1024}MB - may affect connection stability")
            elif msg_size > 10000:
                logger.info(f"[LARGE] App({user_id}): {msg_type or message.get('command')}, ~{msg_size//1000}KB")

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle debug_log from app - log to server, don't forward
            if msg_type == "debug_log":
                tag = message.get("tag", "APP")
                msg = message.get("message", "")
                logger.info(f"[APP-DEBUG][{tag}] {msg}")
                continue

            # Handle get_status request
            if msg_type == "get_status":
                device_id = message.get("device_id")
                if device_id:
                    device_paired = manager.get_device_owner(device_id) == user_id
                    robot_online = manager.is_robot_online(device_id)
                    await websocket.send_json({
                        "type": "status_response",
                        "device_id": device_id,
                        "device_paired": device_paired,
                        "robot_online": robot_online
                    })
                    logger.info(f"[ROUTE] App({user_id}) get_status: device={device_id}, paired={device_paired}, online={robot_online}")
                continue

            # Handle WebRTC signaling from app
            if msg_type == "webrtc_request":
                await handle_webrtc_request(websocket, message, user_id, manager)
                continue

            if msg_type == "webrtc_answer":
                await handle_webrtc_answer(message, user_id, manager)
                continue

            if msg_type == "webrtc_ice":
                await handle_webrtc_ice(message, "app", user_id, manager)
                continue

            if msg_type == "webrtc_close":
                await handle_webrtc_close(message, manager)
                continue

            # Handle commands to robots
            if "command" in message:
                cmd_type = message.get("command")

                # Rate limit check (app-to-robot commands only)
                rate_limit_reason = manager.check_rate_limit(user_id, cmd_type, ip=client_ip)
                if rate_limit_reason:
                    await websocket.send_json({
                        "type": "error",
                        "code": "RATE_LIMITED",
                        "message": rate_limit_reason
                    })
                    continue

                # Skip stale check for upload commands (large files take time to prepare)
                is_upload_cmd = cmd_type in ("upload_song", "audio_upload", "upload_audio", "upload_file")
                if is_upload_cmd:
                    filename = message.get("filename", message.get("data", {}).get("filename", "unknown"))
                    logger.info(f"[UPLOAD] Received {cmd_type} from App({user_id}): {filename}, size={msg_size//1024}KB")

                # Check for stale commands (older than 2 seconds) - skip for uploads
                if not is_upload_cmd:
                    stale, age_ms = is_stale_command(message)
                    if stale:
                        logger.warning(f"[STALE] Dropping stale command from App({user_id}): {cmd_type} (age={age_ms}ms)")
                        await websocket.send_json({
                            "type": "error",
                            "code": "STALE_COMMAND",
                            "message": f"Command too old ({age_ms}ms), dropped"
                        })
                        continue

                # Get target device - check "device_id" first, then "target_device"
                target_device = message.get("device_id") or message.get("target_device")
                # Remove routing fields before forwarding to robot
                message.pop("device_id", None)
                message.pop("target_device", None)

                if not target_device:
                    # Default to first owned device
                    user_devices = manager.get_user_devices(user_id)
                    if user_devices:
                        target_device = user_devices[0]
                        logger.info(f"[ROUTE] No device specified, defaulting to: {target_device}")

                if not target_device:
                    await websocket.send_json({
                        "type": "error",
                        "code": "NO_DEVICE",
                        "message": "No target device specified and no devices paired"
                    })
                    logger.warning(f"[ROUTE] App({user_id}) -> ???: {cmd_type} (no device)")
                    continue

                # Forward command to robot
                success = await manager.forward_command_to_robot(user_id, target_device, message, ip=client_ip)

                if success:
                    logger.info(f"[ROUTE] App({user_id}) -> Robot({target_device}): {cmd_type}")
                    # Build 41: Enhanced logging for mission commands
                    if cmd_type == "start_mission":
                        mission_type = message.get("mission_type", message.get("data", {}).get("mission_type", "unknown"))
                        logger.info(f"[MISSION-CMD] start_mission sent to {target_device}: mission_type={mission_type}")
                else:
                    # Check why it failed
                    if not manager.is_robot_online(target_device):
                        await websocket.send_json({
                            "type": "error",
                            "code": "DEVICE_OFFLINE",
                            "message": f"Device {target_device} is offline"
                        })
                        logger.warning(f"[ROUTE] App({user_id}) -> Robot({target_device}): {cmd_type} FAILED (offline)")
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "code": "FORWARD_FAILED",
                            "message": f"Failed to forward command to device {target_device}"
                        })
                        logger.warning(f"[ROUTE] App({user_id}) -> Robot({target_device}): {cmd_type} FAILED (not authorized)")

    except WebSocketDisconnect:
        logger.info(f"App disconnected for user {user_id}")
    except Exception as e:
        logger.error(f"Error in app websocket for user {user_id}: {e}")
    finally:
        # Collect WebRTC session data for this websocket BEFORE removing
        saved_sessions = []
        for session_id, session in webrtc_sessions.items():
            if session.get("app_ws") == websocket:
                saved_sessions.append((session_id, session.get("device_id")))

        # Disconnect this websocket from the manager
        await manager.disconnect_app(websocket)

        # Check if user still has other active connections
        has_other_connections = (
            user_id in manager.app_connections
            and len(manager.app_connections[user_id]) > 0
        )

        if has_other_connections:
            # User still connected on another session — clean up this WS's sessions normally
            for session_id, device_id in saved_sessions:
                webrtc_sessions.pop(session_id, None)
                if device_id and manager.webrtc_sessions.get(device_id) == session_id:
                    del manager.webrtc_sessions[device_id]
                    logger.info(f"[WEBRTC] Removed session {session_id} for device {device_id} (app has other connections)")
        elif user_id in manager.grace_timers:
            # Already in grace period (e.g., another WS just disconnected) — append sessions
            manager.grace_webrtc_sessions.setdefault(user_id, []).extend(saved_sessions)
            logger.info(f"[GRACE] Appended {len(saved_sessions)} session(s) to existing grace period for user {user_id}")
        else:
            # Last connection — start grace period
            manager.start_grace_period(user_id, saved_sessions)


@router.websocket("/ws")
async def websocket_generic_endpoint(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
    manager: ConnectionManager = Depends(get_connection_manager)
):
    """
    Generic WebSocket endpoint that handles authentication via message.
    Supports both robot and app connections with in-band auth.
    """
    await websocket.accept()

    authenticated = False
    connection_type = None
    identifier = None  # device_id for robot, user_id for app
    client_ip = get_client_ip(websocket)

    try:
        # Wait for auth message
        data = await websocket.receive_text()

        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            await websocket.send_json({
                "type": "auth_result",
                "success": False,
                "message": "Invalid JSON"
            })
            await websocket.close(code=4000)
            return

        if message.get("type") != "auth":
            await websocket.send_json({
                "type": "auth_result",
                "success": False,
                "message": "First message must be auth"
            })
            await websocket.close(code=4000)
            return

        token = message.get("token")
        device_id = message.get("device_id")

        # Determine if this is a robot or app connection
        if device_id:
            # Robot connection - token is HMAC signature
            if not verify_device_signature(device_id, token, settings.device_secret):
                await websocket.send_json({
                    "type": "auth_result",
                    "success": False,
                    "message": "Invalid device signature"
                })
                await websocket.close(code=4001)
                return

            connection_type = "robot"
            identifier = device_id

            # Get owner from database (authoritative source)
            owner_id = db_get_device_owner(device_id)
            if owner_id:
                logger.info(f"Device {device_id} paired to user {owner_id} (from DB)")
            else:
                logger.warning(f"Device {device_id} has no owner in database")

            # Register in manager (but don't call accept() again)
            if device_id in manager.robot_connections:
                old_ws = manager.robot_connections[device_id]
                await manager.disconnect_robot(old_ws)

            manager.robot_connections[device_id] = websocket
            manager.connection_metadata[websocket] = {
                "type": "robot",
                "device_id": device_id,
                "connected_at": datetime.now(timezone.utc),
                "ip": client_ip,
            }
            if owner_id:
                manager.device_owners[device_id] = owner_id

            logger.info(f"Robot {device_id} connected from {client_ip} (generic endpoint)")
            update_device_online_status(device_id, True)

        else:
            # App connection - token is JWT
            payload = decode_token(token, settings)
            if not payload:
                await websocket.send_json({
                    "type": "auth_result",
                    "success": False,
                    "message": "Invalid or expired token"
                })
                await websocket.close(code=4001)
                return

            user_id = payload.get("sub")
            if not user_id:
                await websocket.send_json({
                    "type": "auth_result",
                    "success": False,
                    "message": "Invalid token payload"
                })
                await websocket.close(code=4001)
                return

            connection_type = "app"
            identifier = user_id

            # Register in manager
            if user_id not in manager.app_connections:
                manager.app_connections[user_id] = []
            manager.app_connections[user_id].append(websocket)
            manager.connection_metadata[websocket] = {
                "type": "app",
                "user_id": user_id,
                "connected_at": datetime.now(timezone.utc),
                "ip": client_ip,
            }
            logger.info(f"App session connected for user {user_id} from {client_ip} (generic endpoint)")

        authenticated = True
        await websocket.send_json({
            "type": "auth_result",
            "success": True
        })

        # Send initial data for app connections
        if connection_type == "app":
            # Check for grace period reconnection
            restored_sessions = manager.cancel_grace_period(identifier)
            if restored_sessions:
                for session_id, dev_id in restored_sessions:
                    if session_id in webrtc_sessions:
                        webrtc_sessions[session_id]["app_ws"] = websocket
                        logger.info(f"[GRACE] Restored WebRTC session {session_id} for device {dev_id}")
                        await websocket.send_json({
                            "type": "session_restored",
                            "session_id": session_id,
                            "device_id": dev_id,
                        })
                logger.info(f"[GRACE] User {identifier} reconnected, restored {len(restored_sessions)} session(s)")

            # Send current status of user's paired devices and notify robots that user is online
            user_devices = manager.get_user_devices(identifier)
            for device_id in user_devices:
                await websocket.send_json({
                    "type": "robot_status",
                    "device_id": device_id,
                    "online": manager.is_robot_online(device_id)
                })
                # Notify robot that user has connected (if robot is online)
                if manager.is_robot_online(device_id):
                    await manager.send_to_robot(device_id, {
                        "type": "user_connected",
                        "user_id": identifier
                    })
                    logger.info(f"[CONNECT] Sent user_connected to robot {device_id} for user {identifier} from {client_ip}")

            # Send today's metrics for each of the user's dogs
            try:
                user_dogs_list = get_user_dogs(identifier)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                for dog in user_dogs_list:
                    dog_metrics = db_get_metrics(dog["id"], identifier, today)
                    dog_metrics.pop("dog_id", None)
                    await websocket.send_json({
                        "type": "metrics_sync",
                        "dog_id": dog["id"],
                        "metrics": dog_metrics,
                    })
            except Exception as e:
                logger.error(f"[METRICS] Failed to send metrics_sync to user {identifier}: {e}")

        # Main message loop
        while True:
            data = await websocket.receive_text()

            # Track activity for grace period (app connections)
            if connection_type == "app":
                manager.update_activity(identifier)

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = message.get("type")

            # Log large payloads with connection health monitoring (P1: Build 34)
            msg_size = len(data)

            # Reject messages over 1MB from apps - use HTTP upload instead (Build 38)
            if connection_type == "app" and msg_size > MAX_WEBSOCKET_MESSAGE_SIZE:
                logger.warning(f"[REJECTED] Message too large from App({identifier}): {msg_size//1024}KB, type={msg_type or message.get('command')}")
                await websocket.send_json({
                    "type": "error",
                    "code": "MESSAGE_TOO_LARGE",
                    "message": f"Message too large ({msg_size//1024}KB). Use HTTP upload for files over 1MB. See POST /api/music/upload"
                })
                continue

            if msg_size > LARGE_MESSAGE_THRESHOLD:
                logger.warning(f"[LARGE-MSG] {connection_type}({identifier}): {msg_type or message.get('command')}, {msg_size//1024//1024}MB - may affect connection stability")
            elif msg_size > 10000:
                logger.info(f"[LARGE] {connection_type}({identifier}): {msg_type or message.get('command')}, ~{msg_size//1000}KB")

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle WebRTC signaling
            if connection_type == "robot":
                if msg_type == "webrtc_offer":
                    await handle_webrtc_offer(message, identifier, manager)
                    continue
                if msg_type == "webrtc_ice":
                    await handle_webrtc_ice(message, "robot", identifier, manager)
                    continue
                if msg_type == "webrtc_close":
                    await handle_webrtc_close(message, manager)
                    continue

                # Handle status_update from robot
                if msg_type == "status_update":
                    if "device_id" not in message:
                        message["device_id"] = identifier
                    owner_id = manager.get_device_owner(identifier)
                    if owner_id:
                        await manager.send_to_user_apps(owner_id, message)
                        logger.info(f"[ROUTE] Robot({identifier}) -> App({owner_id}): status_update")
                    continue

                # Handle upload result events from robot (Build 34 Task 2)
                if msg_type in ("upload_complete", "upload_error", "upload_result"):
                    if "device_id" not in message:
                        message["device_id"] = identifier
                    if "timestamp" not in message:
                        message["timestamp"] = datetime.now(timezone.utc).isoformat()

                    owner_id = manager.get_device_owner(identifier)
                    filename = message.get("filename", "unknown")
                    success = message.get("success", False)
                    error = message.get("error")

                    if success:
                        logger.info(f"[UPLOAD] Success from {identifier}: {filename}")
                    else:
                        logger.warning(f"[UPLOAD] Failed from {identifier}: {filename} - {error}")

                    if owner_id:
                        delivered = await manager.send_to_user_apps(owner_id, message)
                        if delivered > 0:
                            logger.info(f"[EVENT-OK] {msg_type} delivered to {delivered} app(s)")
                        else:
                            logger.warning(f"[EVENT-FAIL] {msg_type} NOT delivered - no apps connected")
                    continue

                # Handle audio_state events from robot (Build 34 Task 3)
                if msg_type == "audio_state":
                    if "device_id" not in message:
                        message["device_id"] = identifier

                    owner_id = manager.get_device_owner(identifier)
                    state = message.get("state", "unknown")
                    logger.info(f"[AUDIO] State from {identifier}: {state}")

                    if owner_id:
                        delivered = await manager.send_to_user_apps(owner_id, message)
                        if delivered > 0:
                            logger.info(f"[EVENT-OK] audio_state delivered to {delivered} app(s)")
                        else:
                            logger.warning(f"[EVENT-FAIL] audio_state NOT delivered")
                    continue

                # Handle schedule events from robot (Build 41 - Schedule Event Verification)
                if msg_type in ("schedule_created", "schedule_updated", "schedule_deleted"):
                    if "device_id" not in message:
                        message["device_id"] = identifier
                    if "timestamp" not in message:
                        message["timestamp"] = datetime.now(timezone.utc).isoformat()

                    owner_id = manager.get_device_owner(identifier)
                    schedule_id = message.get("schedule_id", message.get("data", {}).get("schedule_id", "unknown"))

                    logger.info(f"[SCHEDULE] {msg_type} from {identifier}: schedule_id={schedule_id}")

                    if owner_id:
                        delivered = await manager.send_to_user_apps(owner_id, message)
                        if delivered > 0:
                            logger.info(f"[SCHEDULE-OK] {msg_type} delivered to {delivered} app(s)")
                        else:
                            logger.warning(f"[SCHEDULE-FAIL] {msg_type} NOT delivered - no apps connected")
                    else:
                        logger.warning(f"[SCHEDULE] {msg_type} from {identifier} - no owner found")
                    continue

                # Forward events to owner's apps (legacy "event" field format)
                if "event" in message:
                    if "timestamp" not in message:
                        message["timestamp"] = datetime.now(timezone.utc).isoformat()
                    if "device_id" not in message:
                        message["device_id"] = identifier

                    event_name = message.get("event")
                    owner_id = manager.get_device_owner(identifier)

                    # Enhanced logging for mission events
                    if event_name == "mission_progress":
                        data = message.get("data", {})
                        logger.info(f"[MISSION] Progress event from {identifier}: status={data.get('status')} "
                                    f"stage={data.get('stage')}/{data.get('total_stages')} "
                                    f"mission_type={data.get('mission_type')}")
                        # Build 40: Warn if mission data is empty (diagnostic for debugging)
                        if not data.get("status") and not data.get("action"):
                            logger.warning(
                                f"[MISSION] Empty progress data from {identifier} — "
                                f"fields: status={data.get('status')}, action={data.get('action')}, "
                                f"stage={data.get('stage_number')}/{data.get('total_stages')}"
                            )
                    elif event_name in ("mode_changed", "bark_detected", "dog_detected", "treat_dispensed"):
                        logger.info(f"[EVENT] {event_name} from {identifier}: {message.get('data', {})}")

                    # Forward event and verify delivery (P2: Build 34 - Event Forwarding Verification)
                    delivered_count = await manager.forward_event_to_owner(identifier, message)
                    if event_name in TRACKED_EVENTS:
                        if delivered_count > 0:
                            logger.info(f"[EVENT-OK] {event_name} delivered to {delivered_count} app(s) for user {owner_id}")
                        else:
                            logger.warning(f"[EVENT-FAIL] {event_name} NOT delivered - user {owner_id} has no connected apps")
                    else:
                        logger.info(f"[ROUTE] Robot({identifier}) -> App({owner_id}): event={event_name}")
                    continue

                # Handle metric_event from robot
                if msg_type == "metric_event":
                    owner_id = manager.get_device_owner(identifier)
                    if owner_id:
                        dog_id = message.get("dog_id")
                        metric_type = message.get("metric_type")
                        value = message.get("value", 1)
                        mission_type = message.get("mission_type")
                        mission_result = message.get("mission_result")
                        details = message.get("details")

                        if dog_id and mission_type and mission_result:
                            db_log_mission(dog_id, owner_id, mission_type, mission_result, details)
                        elif dog_id and metric_type:
                            try:
                                db_log_metric(dog_id, owner_id, metric_type, value)
                            except ValueError as e:
                                logger.warning(f"[METRICS] Invalid metric from robot {identifier}: {e}")
                                continue

                        message["device_id"] = identifier
                        await manager.send_to_user_apps(owner_id, message)
                        logger.info(f"[ROUTE] Robot({identifier}) -> App({owner_id}): metric_event")
                    continue

                # Catch-all: forward any other type-based message to owner's apps
                if msg_type:
                    if "device_id" not in message:
                        message["device_id"] = identifier
                    owner_id = manager.get_device_owner(identifier)
                    if owner_id:
                        await manager.send_to_user_apps(owner_id, message)
                        logger.info(f"[ROUTE] Robot({identifier}) -> App({owner_id}): {msg_type}")
                    else:
                        logger.warning(f"[ROUTE] Robot({identifier}) -> ???: {msg_type} (no owner)")

            elif connection_type == "app":
                # Handle debug_log from app - log to server, don't forward
                if msg_type == "debug_log":
                    tag = message.get("tag", "APP")
                    msg = message.get("message", "")
                    logger.info(f"[APP-DEBUG][{tag}] {msg}")
                    continue

                # Handle get_status request
                if msg_type == "get_status":
                    device_id = message.get("device_id")
                    if device_id:
                        device_paired = manager.get_device_owner(device_id) == identifier
                        robot_online = manager.is_robot_online(device_id)
                        await websocket.send_json({
                            "type": "status_response",
                            "device_id": device_id,
                            "device_paired": device_paired,
                            "robot_online": robot_online
                        })
                        logger.info(f"[ROUTE] App({identifier}) get_status: device={device_id}, paired={device_paired}, online={robot_online}")
                    continue

                if msg_type == "webrtc_request":
                    await handle_webrtc_request(websocket, message, identifier, manager)
                    continue
                if msg_type == "webrtc_answer":
                    await handle_webrtc_answer(message, identifier, manager)
                    continue
                if msg_type == "webrtc_ice":
                    await handle_webrtc_ice(message, "app", identifier, manager)
                    continue
                if msg_type == "webrtc_close":
                    await handle_webrtc_close(message, manager)
                    continue

                # Forward commands to robot
                if "command" in message:
                    cmd_type = message.get("command")

                    # Rate limit check (app-to-robot commands only)
                    rate_limit_reason = manager.check_rate_limit(identifier, cmd_type, ip=client_ip)
                    if rate_limit_reason:
                        await websocket.send_json({
                            "type": "error",
                            "code": "RATE_LIMITED",
                            "message": rate_limit_reason
                        })
                        continue

                    # Skip stale check for upload commands (large files take time to prepare)
                    is_upload_cmd = cmd_type in ("upload_song", "audio_upload", "upload_audio", "upload_file")
                    if is_upload_cmd:
                        filename = message.get("filename", message.get("data", {}).get("filename", "unknown"))
                        logger.info(f"[UPLOAD] Received {cmd_type} from App({identifier}): {filename}, size={msg_size//1024}KB")

                    # Check for stale commands (older than 2 seconds) - skip for uploads
                    if not is_upload_cmd:
                        stale, age_ms = is_stale_command(message)
                        if stale:
                            logger.warning(f"[STALE] Dropping stale command from App({identifier}): {cmd_type} (age={age_ms}ms)")
                            await websocket.send_json({
                                "type": "error",
                                "code": "STALE_COMMAND",
                                "message": f"Command too old ({age_ms}ms), dropped"
                            })
                            continue

                    # Get target device - check "device_id" first, then "target_device"
                    target_device = message.get("device_id") or message.get("target_device")
                    message.pop("device_id", None)
                    message.pop("target_device", None)

                    if not target_device:
                        user_devices = manager.get_user_devices(identifier)
                        if user_devices:
                            target_device = user_devices[0]
                            logger.info(f"[ROUTE] No device specified, defaulting to: {target_device}")

                    if target_device:
                        success = await manager.forward_command_to_robot(identifier, target_device, message, ip=client_ip)
                        if success:
                            logger.info(f"[ROUTE] App({identifier}) -> Robot({target_device}): {cmd_type}")
                            # Build 41: Enhanced logging for mission commands
                            if cmd_type == "start_mission":
                                mission_type = message.get("mission_type", message.get("data", {}).get("mission_type", "unknown"))
                                logger.info(f"[MISSION-CMD] start_mission sent to {target_device}: mission_type={mission_type}")
                        else:
                            logger.warning(f"[ROUTE] App({identifier}) -> Robot({target_device}): {cmd_type} FAILED")
                    else:
                        logger.warning(f"[ROUTE] App({identifier}) -> ???: {cmd_type} (no device)")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {connection_type} {identifier}")
    except Exception as e:
        logger.error(f"Error in websocket: {e}")
    finally:
        if authenticated:
            if connection_type == "robot":
                cleanup_sessions_for_device(identifier, manager)
                await manager.disconnect_robot(websocket)
                update_device_online_status(identifier, False)
            elif connection_type == "app":
                # Collect WebRTC session data for this websocket BEFORE removing
                saved_sessions = []
                for session_id, session in webrtc_sessions.items():
                    if session.get("app_ws") == websocket:
                        saved_sessions.append((session_id, session.get("device_id")))

                # Disconnect this websocket from the manager
                await manager.disconnect_app(websocket)

                # Check if user still has other active connections
                has_other_connections = (
                    identifier in manager.app_connections
                    and len(manager.app_connections[identifier]) > 0
                )

                if has_other_connections:
                    for session_id, device_id in saved_sessions:
                        webrtc_sessions.pop(session_id, None)
                        if device_id and manager.webrtc_sessions.get(device_id) == session_id:
                            del manager.webrtc_sessions[device_id]
                            logger.info(f"[WEBRTC] Removed session {session_id} for device {device_id} (app has other connections)")
                elif identifier in manager.grace_timers:
                    manager.grace_webrtc_sessions.setdefault(identifier, []).extend(saved_sessions)
                    logger.info(f"[GRACE] Appended {len(saved_sessions)} session(s) to existing grace period for user {identifier}")
                else:
                    manager.start_grace_period(identifier, saved_sessions)
