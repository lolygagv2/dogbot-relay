import asyncio
import json
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

from app.config import get_settings
from app.database import get_all_device_pairings

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections for both robots and mobile apps.
    Handles message routing between connected devices.
    """

    GRACE_PERIOD_SECONDS = 600  # 10 minutes (increased from 5 for screen lock handling)

    def __init__(self):
        # device_id -> WebSocket connection for robots
        self.robot_connections: dict[str, WebSocket] = {}

        # user_id -> list of WebSocket connections (user can have multiple app sessions)
        self.app_connections: dict[str, list[WebSocket]] = {}

        # device_id -> user_id mapping (which user owns which device)
        # Loaded from database on startup, kept in sync via pairing endpoints
        self.device_owners: dict[str, str] = get_all_device_pairings()

        # WebSocket -> metadata for reverse lookups
        self.connection_metadata: dict[WebSocket, dict] = {}

        # device_id -> session_id: enforce single active WebRTC session per device
        self.webrtc_sessions: dict[str, str] = {}

        # Grace period state for session persistence on phone lock
        # user_id -> asyncio.Task that will clean up after grace period
        self.grace_timers: dict[str, asyncio.Task] = {}
        # user_id -> list of (session_id, device_id) saved WebRTC sessions
        self.grace_webrtc_sessions: dict[str, list[tuple[str, str]]] = {}
        # user_id -> datetime of last activity
        self.last_activity: dict[str, datetime] = {}

        # Rate limiting: user_id -> deque of (timestamp, cmd_type)
        self.command_history: dict[str, deque] = {}

        logger.info(f"ConnectionManager initialized with {len(self.device_owners)} device pairings from DB")
        for device_id, user_id in self.device_owners.items():
            logger.info(f"  {device_id} -> {user_id}")

    async def connect_robot(self, websocket: WebSocket, device_id: str, owner_id: Optional[str] = None, ip: str = "unknown"):
        """Register a robot WebSocket connection."""
        await websocket.accept()

        # Disconnect existing connection for this device if any
        if device_id in self.robot_connections:
            old_ws = self.robot_connections[device_id]
            await self.disconnect_robot(old_ws)

        self.robot_connections[device_id] = websocket
        self.connection_metadata[websocket] = {
            "type": "robot",
            "device_id": device_id,
            "connected_at": datetime.now(timezone.utc),
            "ip": ip,
        }

        if owner_id:
            self.device_owners[device_id] = owner_id

        logger.info(f"Robot {device_id} connected from {ip}")

    async def connect_app(self, websocket: WebSocket, user_id: str, ip: str = "unknown"):
        """Register a mobile app WebSocket connection."""
        await websocket.accept()

        if user_id not in self.app_connections:
            self.app_connections[user_id] = []

        self.app_connections[user_id].append(websocket)
        self.connection_metadata[websocket] = {
            "type": "app",
            "user_id": user_id,
            "connected_at": datetime.now(timezone.utc),
            "ip": ip,
        }

        # Debug: log device ownership state
        user_devices = self.get_user_devices(user_id)
        logger.info(f"App session connected for user {user_id} from {ip}")
        logger.info(f"  -> device_owners: {self.device_owners}")
        logger.info(f"  -> user's devices: {user_devices}")

    async def disconnect_robot(self, websocket: WebSocket):
        """Remove a robot WebSocket connection."""
        metadata = self.connection_metadata.get(websocket)
        if metadata and metadata["type"] == "robot":
            device_id = metadata["device_id"]
            ip = metadata.get("ip", "unknown")
            if device_id in self.robot_connections:
                del self.robot_connections[device_id]
            logger.info(f"Robot {device_id} disconnected (was {ip})")

        if websocket in self.connection_metadata:
            del self.connection_metadata[websocket]

        try:
            await websocket.close()
        except Exception:
            pass

    async def disconnect_app(self, websocket: WebSocket):
        """Remove a mobile app WebSocket connection."""
        metadata = self.connection_metadata.get(websocket)
        if metadata and metadata["type"] == "app":
            user_id = metadata["user_id"]
            ip = metadata.get("ip", "unknown")
            if user_id in self.app_connections:
                self.app_connections[user_id] = [
                    ws for ws in self.app_connections[user_id] if ws != websocket
                ]
                if not self.app_connections[user_id]:
                    del self.app_connections[user_id]
            logger.info(f"App session disconnected for user {user_id} (was {ip})")

        if websocket in self.connection_metadata:
            del self.connection_metadata[websocket]

        try:
            await websocket.close()
        except Exception:
            pass

    def update_activity(self, user_id: str):
        """Update last activity timestamp for a user."""
        self.last_activity[user_id] = datetime.now(timezone.utc)

    def check_rate_limit(self, user_id: str, cmd_type: str, ip: str = "unknown") -> Optional[str]:
        """
        Check if a command should be rate-limited.
        Returns an error reason string if blocked, or None if allowed.
        Also logs a warning for suspicious command diversity (forensic only, does not block).
        """
        import time
        settings = get_settings()
        now = time.monotonic()

        if user_id not in self.command_history:
            self.command_history[user_id] = deque()

        history = self.command_history[user_id]

        # Prune entries older than the rate limit window
        window_cutoff = now - settings.rate_limit_window_seconds
        while history and history[0][0] < window_cutoff:
            history.popleft()

        # Check count-based rate limit
        if len(history) >= settings.rate_limit_max_commands:
            logger.warning(
                f"[RATE-LIMIT] BLOCKED user={user_id} ip={ip} cmd={cmd_type} "
                f"count={len(history)}/{settings.rate_limit_max_commands} "
                f"window={settings.rate_limit_window_seconds}s"
            )
            return (
                f"Rate limited: {len(history)} commands in {settings.rate_limit_window_seconds}s "
                f"(max {settings.rate_limit_max_commands})"
            )

        # Record this command
        history.append((now, cmd_type))

        # Check command diversity (forensic warning only, does not block)
        diversity_cutoff = now - settings.rate_limit_diversity_window
        recent_types = set()
        for ts, ct in history:
            if ts >= diversity_cutoff:
                recent_types.add(ct)

        if len(recent_types) >= settings.rate_limit_diversity_threshold:
            logger.warning(
                f"[RATE-LIMIT] SUSPICIOUS DIVERSITY user={user_id} ip={ip} "
                f"types={sorted(recent_types)} in {settings.rate_limit_diversity_window}s "
                f"({len(recent_types)} distinct)"
            )

        return None

    def start_grace_period(self, user_id: str, webrtc_session_data: list[tuple[str, str]]):
        """
        Start a grace period for a disconnected user.
        Saves WebRTC session data and schedules cleanup after GRACE_PERIOD_SECONDS.
        """
        # Cancel existing timer if any
        if user_id in self.grace_timers:
            self.grace_timers[user_id].cancel()
            logger.info(f"[GRACE] Cancelled existing grace timer for user {user_id}")

        # Save or extend WebRTC sessions
        if user_id in self.grace_webrtc_sessions:
            self.grace_webrtc_sessions[user_id].extend(webrtc_session_data)
        else:
            self.grace_webrtc_sessions[user_id] = list(webrtc_session_data)

        # Schedule cleanup
        task = asyncio.create_task(self._execute_grace_cleanup(user_id))
        self.grace_timers[user_id] = task
        logger.info(f"[GRACE] Started {self.GRACE_PERIOD_SECONDS}s grace period for user {user_id} "
                     f"(preserving {len(webrtc_session_data)} WebRTC session(s))")

    def cancel_grace_period(self, user_id: str) -> Optional[list[tuple[str, str]]]:
        """
        Cancel grace period for a reconnecting user.
        Returns saved WebRTC session data if any, or None.
        """
        if user_id not in self.grace_timers:
            return None

        self.grace_timers[user_id].cancel()
        del self.grace_timers[user_id]

        saved_sessions = self.grace_webrtc_sessions.pop(user_id, None)
        logger.info(f"[GRACE] Cancelled grace period for user {user_id} "
                     f"(restoring {len(saved_sessions) if saved_sessions else 0} WebRTC session(s))")
        return saved_sessions

    async def _execute_grace_cleanup(self, user_id: str):
        """Execute cleanup after grace period expires."""
        try:
            await asyncio.sleep(self.GRACE_PERIOD_SECONDS)
        except asyncio.CancelledError:
            logger.info(f"[GRACE] Grace cleanup cancelled for user {user_id} (reconnected)")
            return

        logger.info(f"[GRACE] Grace period expired for user {user_id}, cleaning up")

        # Clean up saved WebRTC sessions
        saved_sessions = self.grace_webrtc_sessions.pop(user_id, [])
        for session_id, device_id in saved_sessions:
            if device_id and self.webrtc_sessions.get(device_id) == session_id:
                del self.webrtc_sessions[device_id]
                # Notify robot to close session
                await self.send_to_robot(device_id, {
                    "type": "webrtc_close",
                    "session_id": session_id
                })
                logger.info(f"[GRACE] Closed WebRTC session {session_id} for device {device_id}")

        # Notify all user's robots that the user has disconnected
        # This allows robots to clear any user-specific state
        user_devices = self.get_user_devices(user_id)
        for device_id in user_devices:
            await self.send_to_robot(device_id, {
                "type": "user_disconnected",
                "user_id": user_id
            })
            logger.info(f"[GRACE] Sent user_disconnected to robot {device_id}")

        # Remove timer reference and rate limit history
        self.grace_timers.pop(user_id, None)
        self.last_activity.pop(user_id, None)
        self.command_history.pop(user_id, None)

        logger.info(f"[GRACE] Cleanup complete for user {user_id}")

    async def send_to_robot(self, device_id: str, message: dict) -> bool:
        """Send a message to a specific robot."""
        websocket = self.robot_connections.get(device_id)
        if websocket:
            try:
                await websocket.send_json(message)
                msg_type = message.get("type") or message.get("command") or "unknown"
                logger.info(f"[SEND->ROBOT] {device_id}: {msg_type}")
                return True
            except Exception as e:
                logger.error(f"Failed to send to robot {device_id}: {e}")
                await self.disconnect_robot(websocket)
        else:
            logger.warning(f"[SEND->ROBOT] {device_id}: robot not connected")
        return False

    async def send_to_user_apps(self, user_id: str, message: dict) -> int:
        """Send a message to all app sessions for a user. Returns count of successful sends."""
        connections = self.app_connections.get(user_id, [])
        sent_count = 0
        failed = []

        for websocket in connections:
            try:
                await websocket.send_json(message)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send to app for user {user_id}: {e}")
                failed.append(websocket)

        # Clean up failed connections
        for ws in failed:
            await self.disconnect_app(ws)

        msg_type = message.get("type") or message.get("event") or "unknown"
        if sent_count > 0:
            logger.info(f"[SEND->APP] {user_id}: {msg_type} (sent to {sent_count} session(s))")
        else:
            logger.warning(f"[SEND->APP] {user_id}: {msg_type} - no app sessions connected")

        return sent_count

    async def forward_command_to_robot(
        self,
        user_id: str,
        device_id: str,
        command: dict,
        ip: str = "unknown",
    ) -> bool:
        """
        Forward a command from app to robot.
        Validates that the user owns the device.
        """
        cmd_type = command.get("command") or command.get("type") or "unknown"
        logger.info(f"[ROUTE] App({user_id})@{ip} -> Robot({device_id}): {cmd_type}")

        # Check if user owns this device
        owner = self.device_owners.get(device_id)
        if owner != user_id:
            logger.warning(f"[ROUTE DENIED] User {user_id} not authorized for device {device_id} (owner: {owner})")
            return False

        return await self.send_to_robot(device_id, command)

    async def forward_event_to_owner(self, device_id: str, event: dict) -> int:
        """
        Forward an event from robot to the owner's apps.
        Returns count of apps that received the message.
        """
        event_type = event.get("event") or event.get("type") or "unknown"
        owner_id = self.device_owners.get(device_id)

        if not owner_id:
            logger.warning(f"[ROUTE] Robot({device_id}) -> ??? : {event_type} - no owner found")
            return 0

        logger.info(f"[ROUTE] Robot({device_id}) -> App({owner_id}): {event_type}")
        return await self.send_to_user_apps(owner_id, event)

    def set_device_owner(self, device_id: str, user_id: str):
        """Associate a device with an owner."""
        self.device_owners[device_id] = user_id
        logger.info(f"Device {device_id} assigned to user {user_id}")

    def remove_device_owner(self, device_id: str) -> bool:
        """Remove device ownership. Returns True if device was paired."""
        if device_id in self.device_owners:
            del self.device_owners[device_id]
            logger.info(f"Device {device_id} unpaired")
            return True
        return False

    def get_device_owner(self, device_id: str) -> Optional[str]:
        """Get the owner of a device."""
        return self.device_owners.get(device_id)

    def is_robot_online(self, device_id: str) -> bool:
        """Check if a robot is currently connected."""
        return device_id in self.robot_connections

    def is_user_online(self, user_id: str) -> bool:
        """Check if a user has any active app connections or is in grace period."""
        has_connections = user_id in self.app_connections and len(self.app_connections[user_id]) > 0
        in_grace = user_id in self.grace_timers
        return has_connections or in_grace

    def get_user_devices(self, user_id: str) -> list[str]:
        """Get all device IDs owned by a user."""
        return [
            device_id for device_id, owner in self.device_owners.items()
            if owner == user_id
        ]

    def get_stats(self) -> dict:
        """Get connection statistics."""
        return {
            "robots_online": len(self.robot_connections),
            "users_online": len(self.app_connections),
            "total_app_sessions": sum(len(sessions) for sessions in self.app_connections.values()),
            "registered_devices": len(self.device_owners),
            "active_webrtc_sessions": len(self.webrtc_sessions),
            "grace_period_users": len(self.grace_timers),
        }

    async def create_webrtc_session(self, device_id: str, user_id: str) -> str:
        """Create a new WebRTC session for a device, closing any existing session first."""
        if device_id in self.webrtc_sessions:
            old_session_id = self.webrtc_sessions[device_id]
            logger.warning(f"[WEBRTC] Closing existing session {old_session_id} for device {device_id} before creating new one")
            await self.close_webrtc_session(old_session_id, device_id)

        new_session_id = str(uuid.uuid4())
        self.webrtc_sessions[device_id] = new_session_id
        logger.info(f"[WEBRTC] Created session {new_session_id} for device {device_id} (user {user_id})")
        logger.info(f"[WEBRTC] Active sessions: {self.webrtc_sessions}")
        return new_session_id

    async def close_webrtc_session(self, session_id: str, device_id: str = None):
        """Close a WebRTC session. Only removes and notifies robot if session_id matches the active one."""
        if device_id and self.webrtc_sessions.get(device_id) == session_id:
            del self.webrtc_sessions[device_id]
            logger.info(f"[WEBRTC] Removed active session {session_id} for device {device_id}")

            # Notify robot to close this specific session
            await self.send_to_robot(device_id, {
                "type": "webrtc_close",
                "session_id": session_id
            })
        elif device_id:
            logger.info(f"[WEBRTC] Ignoring close for stale session {session_id} (device {device_id} active session: {self.webrtc_sessions.get(device_id)})")

        logger.info(f"[WEBRTC] Active sessions after close: {self.webrtc_sessions}")


# Global connection manager instance
manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Dependency to get the connection manager."""
    return manager
