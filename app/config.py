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

    # Admin accounts (comma-separated emails). An admin's own password acts as a
    # master password: logging in with any customer's email + the admin password
    # issues a token for that customer's account (no ownership transfer needed).
    admin_emails: str = "morgan@wimzai.com"

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

    # AWS SES (password reset emails)
    aws_ses_access_key_id: str = ""
    aws_ses_secret_access_key: str = ""
    aws_ses_region: str = "us-east-1"
    ses_sender_email: str = "noreply@wimzai.com"

    # Session-report LLM layer (L-REPORT / Workstream B). Thin per-call API layer,
    # no always-on model. Empty key = report generation disabled (scaffold-safe).
    anthropic_api_key: str = ""
    session_report_model: str = "claude-haiku-4-5"
    session_report_max_tokens: int = 512

    # Voice command WAV storage. Empty = <repo>/data/voice_commands (next to the
    # SQLite DB). Must be a persistent path — /tmp is wiped on reboot (2026-07-30).
    voice_storage_dir: str = ""

    # Firebase service-account JSON for FCM push (push contract 2026-07-30).
    # Empty = <repo>/data/firebase-service-account.json; missing file disables push.
    fcm_credentials_path: str = ""

    # Rate limiting (app-to-robot commands)
    rate_limit_window_seconds: int = 60
    rate_limit_max_commands: int = 30
    rate_limit_diversity_window: int = 10  # seconds
    rate_limit_diversity_threshold: int = 6  # distinct command types

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
