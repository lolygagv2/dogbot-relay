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

    # Robot OTA release artifacts (updater contract 2026-08-31, robot c28ed1f).
    # Empty = <repo>/data/releases (next to the SQLite DB). Must be a persistent
    # path — /tmp is wiped on reboot and the manifest rows would outlive the files.
    releases_storage_dir: str = ""

    # How many releases keep their artifact bytes on disk. Older releases keep
    # their manifest row (so the robot gets a truthful answer instead of a
    # never-existed 404) but lose the tarball. Artifacts run ~187MB, so the
    # default holds ~560MB — enough for a robot a couple of versions behind,
    # since the app only ever offers the current latest.
    releases_keep_artifacts: int = 3

    # Rate limiting (app-to-robot commands)
    rate_limit_window_seconds: int = 60
    rate_limit_max_commands: int = 30
    rate_limit_diversity_window: int = 10  # seconds
    rate_limit_diversity_threshold: int = 6  # distinct command types

    # ---- Dec 9 demo build (2026-11-06): event store, settings, MCP, voice agent ----
    # Public base URL of this relay (used to build absolute media/audio URLs).
    public_base_url: str = "https://api.wimzai.com"
    # Shared secret for same-box internal callers (wimz-mcp, wimz-agent). They send
    # `Authorization: Bearer <token>` + `X-Actor` + `X-On-Behalf-Of`. Empty = internal
    # callers get 503, so nothing is silently open.
    internal_api_token: str = ""
    # Default dog timezone for daily roll-ups (per-dog override via settings key
    # `timezone`).
    default_timezone: str = "America/New_York"
    # Replayer defaults (admin-only demo tooling).
    demo_dog_id: str = "demo_dog"
    demo_device_id: str = "demo_robot"
    seeds_dir: str = ""           # empty = <repo>/seeds
    sounds_storage_dir: str = ""  # empty = <repo>/data/sounds
    # Voice agent hook target (relay -> agent, fire-and-forget).
    agent_base_url: str = "http://127.0.0.1:8002"

    # Where the MCP server and voice agent reach the relay's HTTP API (same box).
    relay_base_url: str = "http://127.0.0.1:8000"

    # MCP server (separate process, run_mcp.py)
    mcp_port: int = 8001
    # OAuth issuer / resource base. Empty = public_base_url. For local testing set
    # MCP_PUBLIC_BASE_URL=http://127.0.0.1:8001 (the SDK only allows http on loopback).
    mcp_public_base_url: str = ""
    mcp_access_token_ttl: int = 3600
    mcp_refresh_token_ttl: int = 30 * 24 * 3600
    mcp_allowed_hosts: str = "api.wimzai.com,api.wimzai.com:*,127.0.0.1:8001,localhost:8001"

    # Voice agent (separate process, run_agent.py)
    agent_port: int = 8002
    agent_model: str = "claude-opus-5-5"
    openai_api_key: str = ""
    openai_stt_model: str = "gpt-4o-transcribe"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    agent_audio_dir: str = ""     # empty = <repo>/data/agent_audio
    agent_audio_ttl_seconds: int = 900
    agent_boot_greeting_delay_seconds: int = 20

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
