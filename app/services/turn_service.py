import os
import httpx
from typing import Optional


class TURNService:
    """Generate short-lived TURN credentials from Cloudflare."""

    def __init__(self):
        self.turn_key_id = os.getenv("CLOUDFLARE_TURN_KEY_ID")
        self.turn_api_token = os.getenv("CLOUDFLARE_TURN_API_TOKEN")
        self.base_url = "https://rtc.live.cloudflare.com/v1/turn/keys"

    async def generate_credentials(self, ttl: int = 3600) -> dict:
        """
        Generate short-lived TURN credentials.

        Args:
            ttl: Time-to-live in seconds (default 1 hour)

        Returns:
            dict with iceServers array for RTCPeerConnection
        """
        if not self.turn_key_id or not self.turn_api_token:
            raise ValueError("Cloudflare TURN credentials not configured")

        url = f"{self.base_url}/{self.turn_key_id}/credentials/generate-ice-servers"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.turn_api_token}",
                    "Content-Type": "application/json"
                },
                json={"ttl": ttl}
            )
            response.raise_for_status()
            return response.json()


# Singleton instance
turn_service = TURNService()
