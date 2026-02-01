from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Application
    app_name: str = "WIM-Z Cloud Relay"
    debug: bool = False

    # JWT Configuration
    jwt_secret_key: str = "change-this-to-a-secure-secret-key-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    # Device HMAC secret for registration
    device_secret: str = "change-this-device-secret-in-production"

    # WebSocket settings
    ws_heartbeat_interval: int = 30  # seconds
    ws_connection_timeout: int = 120  # seconds (increased for background apps)
    ws_max_message_size: int = 20 * 1024 * 1024  # 20MB for MP3 uploads
    ws_ping_interval: int = 30  # seconds between pings
    ws_ping_timeout: int = 60  # seconds to wait for pong (increased for screen lock)

    # Cloudflare TURN credentials
    cloudflare_turn_key_id: str = ""
    cloudflare_turn_api_token: str = ""

    # TURN credential TTL (24 hours default for stability)
    turn_credential_ttl: int = 86400  # 24 hours in seconds

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
