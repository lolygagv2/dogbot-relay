import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections for both robots and mobile apps.
    Handles message routing between connected devices.
    """

    def __init__(self):
        # device_id -> WebSocket connection for robots
        self.robot_connections: dict[str, WebSocket] = {}

        # user_id -> list of WebSocket connections (user can have multiple app sessions)
        self.app_connections: dict[str, list[WebSocket]] = {}

        # device_id -> user_id mapping (which user owns which device)
        self.device_owners: dict[str, str] = {}

        # WebSocket -> metadata for reverse lookups
        self.connection_metadata: dict[WebSocket, dict] = {}

    async def connect_robot(self, websocket: WebSocket, device_id: str, owner_id: Optional[str] = None):
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
            "connected_at": datetime.now(timezone.utc)
        }

        if owner_id:
            self.device_owners[device_id] = owner_id

        logger.info(f"Robot {device_id} connected")

    async def connect_app(self, websocket: WebSocket, user_id: str):
        """Register a mobile app WebSocket connection."""
        await websocket.accept()

        if user_id not in self.app_connections:
            self.app_connections[user_id] = []

        self.app_connections[user_id].append(websocket)
        self.connection_metadata[websocket] = {
            "type": "app",
            "user_id": user_id,
            "connected_at": datetime.now(timezone.utc)
        }

        logger.info(f"App session connected for user {user_id}")

    async def disconnect_robot(self, websocket: WebSocket):
        """Remove a robot WebSocket connection."""
        metadata = self.connection_metadata.get(websocket)
        if metadata and metadata["type"] == "robot":
            device_id = metadata["device_id"]
            if device_id in self.robot_connections:
                del self.robot_connections[device_id]
            logger.info(f"Robot {device_id} disconnected")

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
            if user_id in self.app_connections:
                self.app_connections[user_id] = [
                    ws for ws in self.app_connections[user_id] if ws != websocket
                ]
                if not self.app_connections[user_id]:
                    del self.app_connections[user_id]
            logger.info(f"App session disconnected for user {user_id}")

        if websocket in self.connection_metadata:
            del self.connection_metadata[websocket]

        try:
            await websocket.close()
        except Exception:
            pass

    async def send_to_robot(self, device_id: str, message: dict) -> bool:
        """Send a message to a specific robot."""
        websocket = self.robot_connections.get(device_id)
        if websocket:
            try:
                await websocket.send_json(message)
                return True
            except Exception as e:
                logger.error(f"Failed to send to robot {device_id}: {e}")
                await self.disconnect_robot(websocket)
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

        return sent_count

    async def forward_command_to_robot(
        self,
        user_id: str,
        device_id: str,
        command: dict
    ) -> bool:
        """
        Forward a command from app to robot.
        Validates that the user owns the device.
        """
        # Check if user owns this device
        owner = self.device_owners.get(device_id)
        if owner != user_id:
            logger.warning(f"User {user_id} tried to command device {device_id} owned by {owner}")
            return False

        return await self.send_to_robot(device_id, command)

    async def forward_event_to_owner(self, device_id: str, event: dict) -> int:
        """
        Forward an event from robot to the owner's apps.
        Returns count of apps that received the message.
        """
        owner_id = self.device_owners.get(device_id)
        if not owner_id:
            logger.warning(f"No owner found for device {device_id}")
            return 0

        return await self.send_to_user_apps(owner_id, event)

    def set_device_owner(self, device_id: str, user_id: str):
        """Associate a device with an owner."""
        self.device_owners[device_id] = user_id
        logger.info(f"Device {device_id} assigned to user {user_id}")

    def get_device_owner(self, device_id: str) -> Optional[str]:
        """Get the owner of a device."""
        return self.device_owners.get(device_id)

    def is_robot_online(self, device_id: str) -> bool:
        """Check if a robot is currently connected."""
        return device_id in self.robot_connections

    def is_user_online(self, user_id: str) -> bool:
        """Check if a user has any active app connections."""
        return user_id in self.app_connections and len(self.app_connections[user_id]) > 0

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
            "registered_devices": len(self.device_owners)
        }


# Global connection manager instance
manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Dependency to get the connection manager."""
    return manager
