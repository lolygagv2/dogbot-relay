import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.auth import decode_token, verify_device_signature, verify_device_signature_with_timestamp
from app.config import Settings, get_settings
from app.connection_manager import ConnectionManager, get_connection_manager
from app.routers.device import get_device_data, update_device_online_status
from app.services.turn_service import turn_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# Track active WebRTC sessions
# session_id -> {"app_ws": WebSocket, "device_id": str, "user_id": str}
webrtc_sessions: dict[str, dict] = {}


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

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Generate TURN credentials
    try:
        ice_servers = await turn_service.generate_credentials(ttl=3600)
    except Exception as e:
        logger.error(f"TURN credential generation failed: {e}")
        await websocket.send_json({
            "type": "error",
            "code": "TURN_ERROR",
            "message": f"Failed to generate TURN credentials: {str(e)}"
        })
        return

    # Track session
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

    logger.info(f"WebRTC session {session_id} initiated: user {user_id} -> device {device_id}")


async def handle_webrtc_offer(message: dict, device_id: str):
    """Forward WebRTC offer from robot to app."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)

    if session and session["app_ws"] and session["device_id"] == device_id:
        try:
            await session["app_ws"].send_json(message)
            logger.debug(f"Forwarded WebRTC offer for session {session_id}")
        except Exception as e:
            logger.error(f"Failed to forward offer for session {session_id}: {e}")


async def handle_webrtc_answer(message: dict, user_id: str, manager: ConnectionManager):
    """Forward WebRTC answer from app to robot."""
    session_id = message.get("session_id")
    session = webrtc_sessions.get(session_id)

    if session and session["user_id"] == user_id:
        device_id = session["device_id"]
        success = await manager.send_to_robot(device_id, message)
        if success:
            logger.debug(f"Forwarded WebRTC answer for session {session_id}")
        else:
            logger.error(f"Failed to forward answer for session {session_id}")


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
        return

    if from_type == "robot":
        # From robot, forward to app
        if session["device_id"] == identifier and session["app_ws"]:
            try:
                await session["app_ws"].send_json(message)
            except Exception as e:
                logger.error(f"Failed to forward ICE to app: {e}")
    else:
        # From app, forward to robot
        if session["user_id"] == identifier:
            device_id = session["device_id"]
            await manager.send_to_robot(device_id, message)


async def handle_webrtc_close(message: dict, manager: ConnectionManager):
    """Clean up WebRTC session."""
    session_id = message.get("session_id")
    session = webrtc_sessions.pop(session_id, None)

    if session:
        close_msg = {"type": "webrtc_close", "session_id": session_id}

        # Notify app
        if session["app_ws"]:
            try:
                await session["app_ws"].send_json(close_msg)
            except Exception:
                pass

        # Notify robot
        await manager.send_to_robot(session["device_id"], close_msg)

        logger.info(f"WebRTC session {session_id} closed")


def cleanup_sessions_for_websocket(websocket: WebSocket):
    """Remove all WebRTC sessions associated with a websocket."""
    sessions_to_remove = []
    for session_id, session in webrtc_sessions.items():
        if session.get("app_ws") == websocket:
            sessions_to_remove.append(session_id)

    for session_id in sessions_to_remove:
        webrtc_sessions.pop(session_id, None)
        logger.info(f"Cleaned up WebRTC session {session_id} due to disconnect")


def cleanup_sessions_for_device(device_id: str):
    """Remove all WebRTC sessions associated with a device."""
    sessions_to_remove = []
    for session_id, session in webrtc_sessions.items():
        if session.get("device_id") == device_id:
            sessions_to_remove.append(session_id)

    for session_id in sessions_to_remove:
        webrtc_sessions.pop(session_id, None)
        logger.info(f"Cleaned up WebRTC session {session_id} due to device disconnect")


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

    # Get device data and owner
    device = get_device_data(device_id)
    owner_id = device.get("owner_id") if device else None

    # Connect the robot
    await manager.connect_robot(websocket, device_id, owner_id)
    update_device_online_status(device_id, True)

    # Notify owner's apps that robot is online
    if owner_id:
        await manager.send_to_user_apps(owner_id, {
            "event": "device_status",
            "data": {
                "device_id": device_id,
                "is_online": True
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

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

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle WebRTC signaling from robot
            if msg_type == "webrtc_offer":
                await handle_webrtc_offer(message, device_id)
                continue

            if msg_type == "webrtc_ice":
                await handle_webrtc_ice(message, "robot", device_id, manager)
                continue

            if msg_type == "webrtc_close":
                await handle_webrtc_close(message, manager)
                continue

            # Forward events to owner's apps
            if "event" in message:
                # Add timestamp if not present
                if "timestamp" not in message:
                    message["timestamp"] = datetime.now(timezone.utc).isoformat()

                # Add device_id to help apps identify source
                if "device_id" not in message:
                    message["device_id"] = device_id

                await manager.forward_event_to_owner(device_id, message)
                logger.debug(f"Forwarded event from robot {device_id}: {message.get('event')}")

    except WebSocketDisconnect:
        logger.info(f"Robot {device_id} disconnected")
    except Exception as e:
        logger.error(f"Error in robot websocket {device_id}: {e}")
    finally:
        # Clean up WebRTC sessions for this device
        cleanup_sessions_for_device(device_id)

        await manager.disconnect_robot(websocket)
        update_device_online_status(device_id, False)

        # Notify owner's apps that robot is offline
        if owner_id:
            await manager.send_to_user_apps(owner_id, {
                "event": "device_status",
                "data": {
                    "device_id": device_id,
                    "is_online": False
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            })


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

    # Connect the app
    await manager.connect_app(websocket, user_id)

    # Send connection acknowledgment
    await websocket.send_json({
        "type": "auth_result",
        "success": True,
        "user_id": user_id
    })

    # Send current status of user's devices
    user_devices = manager.get_user_devices(user_id)
    for device_id in user_devices:
        await websocket.send_json({
            "event": "device_status",
            "data": {
                "device_id": device_id,
                "is_online": manager.is_robot_online(device_id)
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    try:
        while True:
            # Receive message from app
            data = await websocket.receive_text()

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from app user {user_id}: {data}")
                continue

            msg_type = message.get("type")

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
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
                # Get target device (from message or default to first device)
                target_device = message.pop("target_device", None)

                if not target_device:
                    # Default to first owned device
                    user_devices = manager.get_user_devices(user_id)
                    if user_devices:
                        target_device = user_devices[0]

                if not target_device:
                    await websocket.send_json({
                        "type": "error",
                        "code": "NO_DEVICE",
                        "message": "No target device specified and no devices paired"
                    })
                    continue

                # Forward command to robot
                success = await manager.forward_command_to_robot(user_id, target_device, message)

                if not success:
                    # Check if device is offline
                    if not manager.is_robot_online(target_device):
                        await websocket.send_json({
                            "type": "error",
                            "code": "DEVICE_OFFLINE",
                            "message": f"Device {target_device} is offline"
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "code": "FORWARD_FAILED",
                            "message": f"Failed to forward command to device {target_device}"
                        })
                else:
                    logger.debug(f"Forwarded command from user {user_id} to robot {target_device}: {message.get('command')}")

    except WebSocketDisconnect:
        logger.info(f"App disconnected for user {user_id}")
    except Exception as e:
        logger.error(f"Error in app websocket for user {user_id}: {e}")
    finally:
        # Clean up WebRTC sessions for this websocket
        cleanup_sessions_for_websocket(websocket)
        await manager.disconnect_app(websocket)


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

            # Get owner
            device = get_device_data(device_id)
            owner_id = device.get("owner_id") if device else None

            # Register in manager (but don't call accept() again)
            if device_id in manager.robot_connections:
                old_ws = manager.robot_connections[device_id]
                await manager.disconnect_robot(old_ws)

            manager.robot_connections[device_id] = websocket
            manager.connection_metadata[websocket] = {
                "type": "robot",
                "device_id": device_id,
                "connected_at": datetime.now(timezone.utc)
            }
            if owner_id:
                manager.device_owners[device_id] = owner_id

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
                "connected_at": datetime.now(timezone.utc)
            }

        authenticated = True
        await websocket.send_json({
            "type": "auth_result",
            "success": True
        })

        # Main message loop
        while True:
            data = await websocket.receive_text()

            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = message.get("type")

            # Handle ping/pong
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle WebRTC signaling
            if connection_type == "robot":
                if msg_type == "webrtc_offer":
                    await handle_webrtc_offer(message, identifier)
                    continue
                if msg_type == "webrtc_ice":
                    await handle_webrtc_ice(message, "robot", identifier, manager)
                    continue
                if msg_type == "webrtc_close":
                    await handle_webrtc_close(message, manager)
                    continue

                # Forward events to owner's apps
                if "event" in message:
                    if "timestamp" not in message:
                        message["timestamp"] = datetime.now(timezone.utc).isoformat()
                    if "device_id" not in message:
                        message["device_id"] = identifier
                    await manager.forward_event_to_owner(identifier, message)

            elif connection_type == "app":
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
                    target_device = message.pop("target_device", None)
                    if not target_device:
                        user_devices = manager.get_user_devices(identifier)
                        if user_devices:
                            target_device = user_devices[0]

                    if target_device:
                        await manager.forward_command_to_robot(identifier, target_device, message)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {connection_type} {identifier}")
    except Exception as e:
        logger.error(f"Error in websocket: {e}")
    finally:
        if authenticated:
            if connection_type == "robot":
                cleanup_sessions_for_device(identifier)
                await manager.disconnect_robot(websocket)
                update_device_online_status(identifier, False)
            elif connection_type == "app":
                cleanup_sessions_for_websocket(websocket)
                await manager.disconnect_app(websocket)
