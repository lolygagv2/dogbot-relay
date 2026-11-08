import contextlib
import json
import logging
import sqlite3
import os
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Database path
# WIMZ_DB_PATH overrides the location (tests point it at a temp file so they never open data/wimz.db)
DB_PATH = Path(os.environ.get("WIMZ_DB_PATH") or Path(__file__).parent.parent / "data" / "wimz.db")


def get_connection() -> sqlite3.Connection:
    """Get a database connection with WAL mode and busy timeout."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextlib.contextmanager
def db_connection():
    """Context manager that guarantees connection cleanup on all paths."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


# dog d is owned by robot r's owner (same rule as get_dog_owner / check_dog_access)
_OWNED_BY_ROBOT_OWNER_SQL = """(
    d.id IN (SELECT ud.dog_id FROM user_dogs ud WHERE ud.user_id = r.owner_user_id AND ud.role = 'owner')
    OR (d.user_id = r.owner_user_id
        AND NOT EXISTS (SELECT 1 FROM user_dogs ux WHERE ux.dog_id = d.id AND ux.role = 'owner')))"""


def backup_db_before_rebuild(reason: str) -> Path:
    """Consistent copy of the DB (SQLite online backup, WAL-safe) before any schema rebuild.
    Written next to the DB as wimz.db.bak-<UTC timestamp>-<reason>. Raises on failure so the
    rebuild never runs without a backup."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = DB_PATH.with_name(f"{DB_PATH.name}.bak-{stamp}-{reason}")
    src = sqlite3.connect(str(DB_PATH))
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    logger.info(f"[MIGRATION] backed up {DB_PATH} -> {dest} before schema rebuild ({reason})")
    return dest


def init_db():
    """Initialize the database with required tables."""
    with db_connection() as conn:
        cursor = conn.cursor()

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                name TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Add name column if it doesn't exist (migration for existing DBs)
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if "name" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN name TEXT")

        # Dogs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dogs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                breed TEXT,
                color TEXT,
                profile_photo_url TEXT,
                aruco_marker_id INTEGER,
                visual_features TEXT,
                user_id TEXT,
                weight REAL,
                notes TEXT,
                goals TEXT,
                last_mission_id TEXT,
                photo_version INTEGER DEFAULT 1,
                updated_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Migration: add user_id column to dogs if it doesn't exist
        cursor.execute("PRAGMA table_info(dogs)")
        dog_columns = [col[1] for col in cursor.fetchall()]
        if "user_id" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN user_id TEXT REFERENCES users(id)")
            # Backfill user_id from user_dogs where role='owner'
            cursor.execute("""
                UPDATE dogs SET user_id = (
                    SELECT ud.user_id FROM user_dogs ud
                    WHERE ud.dog_id = dogs.id AND ud.role = 'owner'
                    LIMIT 1
                )
                WHERE user_id IS NULL
            """)
            logger.info("[MIGRATION] Added user_id column to dogs table and backfilled from user_dogs")

        # Migration: add new dog profile fields (Build 50 / Phase 1 A1)
        if "weight" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN weight REAL")
        if "notes" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN notes TEXT")
        if "goals" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN goals TEXT")
        if "last_mission_id" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN last_mission_id TEXT")
        if "photo_version" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN photo_version INTEGER DEFAULT 1")
        if "updated_at" not in dog_columns:
            cursor.execute("ALTER TABLE dogs ADD COLUMN updated_at TEXT")
            # Backfill updated_at from created_at
            cursor.execute("UPDATE dogs SET updated_at = created_at WHERE updated_at IS NULL")
            logger.info("[MIGRATION] Added weight/notes/goals/last_mission_id/photo_version/updated_at to dogs")

        # Index on dogs.user_id for fast lookup
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dogs_user_id ON dogs(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dogs_user_created ON dogs(user_id, created_at)")

        # User-dog relationship (many-to-many)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_dogs (
                user_id TEXT NOT NULL,
                dog_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                PRIMARY KEY (user_id, dog_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (dog_id) REFERENCES dogs(id)
            )
        """)

        # Robots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS robots (
                id TEXT PRIMARY KEY,
                device_id TEXT UNIQUE NOT NULL,
                owner_user_id TEXT,
                name TEXT DEFAULT 'WIM-Z Robot',
                firmware_version TEXT,
                pairing_code TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_user_id) REFERENCES users(id)
            )
        """)

        # Migration: add device_secret column to robots (per-device HMAC secrets)
        cursor.execute("PRAGMA table_info(robots)")
        robot_columns = [col[1] for col in cursor.fetchall()]
        if "device_secret" not in robot_columns:
            cursor.execute("ALTER TABLE robots ADD COLUMN device_secret TEXT")
            logger.info("[MIGRATION] Added device_secret column to robots table")

        # Pairing (2026-11-07): explicit robot <-> dog link. Before this table a robot's
        # dogs were implicitly "every dog its owner owns"; the first creation backfills
        # exactly that so existing robots keep the same dogs.
        cursor.execute("PRAGMA table_info(dogs)")
        if "is_placeholder" not in [col[1] for col in cursor.fetchall()]:
            cursor.execute("ALTER TABLE dogs ADD COLUMN is_placeholder INTEGER NOT NULL DEFAULT 0")
            logger.info("[MIGRATION] Added is_placeholder column to dogs table")
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'device_dogs'")
        device_dogs_existed = cursor.fetchone() is not None
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_dogs (
                device_id   TEXT NOT NULL,
                dog_id      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (device_id, dog_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_device_dogs_dog ON device_dogs(dog_id)")
        if not device_dogs_existed:
            cursor.execute(f"""
                INSERT OR IGNORE INTO device_dogs (device_id, dog_id, created_at)
                SELECT r.device_id, d.id, ? FROM robots r JOIN dogs d
                  ON {_OWNED_BY_ROBOT_OWNER_SQL}
                WHERE r.owner_user_id IS NOT NULL
            """, (datetime.now(timezone.utc).isoformat(),))
            logger.info(f"[MIGRATION] Created device_dogs, backfilled {cursor.rowcount} owner-implied link(s)")

        # Dog photos table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dog_photos (
                id TEXT PRIMARY KEY,
                dog_id TEXT NOT NULL,
                photo_url TEXT NOT NULL,
                is_profile_photo INTEGER DEFAULT 0,
                captured_at TEXT NOT NULL,
                FOREIGN KEY (dog_id) REFERENCES dogs(id)
            )
        """)

        # Dog metrics table (daily aggregates)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dog_metrics (
                dog_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                treat_count INTEGER DEFAULT 0,
                detection_count INTEGER DEFAULT 0,
                mission_attempts INTEGER DEFAULT 0,
                mission_successes INTEGER DEFAULT 0,
                session_minutes REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(dog_id, date),
                FOREIGN KEY (dog_id) REFERENCES dogs(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Mission log table (individual mission events)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mission_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dog_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                mission_type TEXT NOT NULL,
                result TEXT NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (dog_id) REFERENCES dogs(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Mission schedules table (Build 34, updated Build 35)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mission_schedules (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                dog_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                name TEXT,
                type TEXT NOT NULL DEFAULT 'daily',
                hour INTEGER NOT NULL DEFAULT 9,
                minute INTEGER NOT NULL DEFAULT 0,
                end_hour INTEGER,
                end_minute INTEGER,
                weekdays TEXT,
                cooldown_hours INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_run TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (dog_id) REFERENCES dogs(id)
            )
        """)

        # Migration: add new columns if they don't exist
        cursor.execute("PRAGMA table_info(mission_schedules)")
        schedule_columns = [col[1] for col in cursor.fetchall()]
        if "end_hour" not in schedule_columns:
            cursor.execute("ALTER TABLE mission_schedules ADD COLUMN end_hour INTEGER")
            cursor.execute("ALTER TABLE mission_schedules ADD COLUMN end_minute INTEGER")
            cursor.execute("ALTER TABLE mission_schedules ADD COLUMN cooldown_hours INTEGER")
            logger.info("[MIGRATION] Added end_hour, end_minute, cooldown_hours to mission_schedules")

        # Global scheduling enabled flag per user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                scheduling_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Activity events (Phase 3 / A3): per-event log streamed by robots, queryable by app
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                dog_id TEXT,
                type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_events_user_dog_ts
            ON activity_events(user_id, dog_id, timestamp DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_events_user_ts
            ON activity_events(user_id, timestamp DESC)
        """)

        # Migration (L-SYNC / Chain SG): add cleared_at so a "clear" from the app
        # persists durably at the relay. Cleared rows are kept (history/moat) but
        # drop out of the timeline. NULL = not cleared. This is the fix for the
        # "cleared events reappear" bug: the clear now round-trips and survives.
        cursor.execute("PRAGMA table_info(activity_events)")
        activity_cols = [col[1] for col in cursor.fetchall()]
        if "cleared_at" not in activity_cols:
            cursor.execute("ALTER TABLE activity_events ADD COLUMN cleared_at TEXT")
            logger.info("[MIGRATION] Added cleared_at column to activity_events")

        # Voice commands (Phase 2 / A2): per-dog voice clips synced from app to robot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_commands (
                user_id TEXT NOT NULL,
                dog_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'wav',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, dog_id, command_id)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_voice_commands_dog
            ON voice_commands(user_id, dog_id, updated_at DESC)
        """)

        # FCM push tokens (push contract 2026-07-30): one row per app install,
        # upserted by device_token. enabled_types is a JSON array of app-side
        # notification type names ("bark", "treatDispensed", ...).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_tokens (
                device_token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                enabled_types TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_tokens_user
            ON push_tokens(user_id)
        """)

        # Password reset codes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reset_codes_email
            ON password_reset_codes(email, used, expires_at)
        """)

        # Robot OTA releases (updater contract 2026-08-31, robot c28ed1f).
        # One row per uploaded artifact; `filename` points into the releases
        # storage dir. Version strings are opaque — the app compares them with
        # plain inequality against the robot's telemetry sw_version, with no
        # semver ordering, so a version string must never be reused.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS releases (
                version TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                filename TEXT NOT NULL,
                notes TEXT,
                min_updater_version TEXT,
                created_at TEXT NOT NULL,
                pruned_at TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_releases_created
            ON releases(created_at DESC)
        """)

        # Migration: add pruned_at to releases created before retention landed.
        release_columns = {r["name"] for r in cursor.execute("PRAGMA table_info(releases)")}
        if "pruned_at" not in release_columns:
            cursor.execute("ALTER TABLE releases ADD COLUMN pruned_at TEXT")
            logger.info("[MIGRATION] Added pruned_at column to releases table")

        # Device event storage (for offline app retrieval)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_events_device_ts
            ON device_events(device_id, timestamp DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_device_events_type
            ON device_events(device_id, event_type, timestamp DESC)
        """)

        # Migration (replay dedup 2026-07-25): robots replay events buffered
        # during relay outages, each carrying a stable unique id. Store it and
        # enforce uniqueness so a replayed event can never insert twice.
        # Partial index: legacy events without an id (NULL) stay unconstrained.
        cursor.execute("PRAGMA table_info(device_events)")
        device_event_cols = [col[1] for col in cursor.fetchall()]
        if "event_id" not in device_event_cols:
            cursor.execute("ALTER TABLE device_events ADD COLUMN event_id TEXT")
            logger.info("[MIGRATION] Added event_id column to device_events")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_device_events_event_id
            ON device_events(event_id) WHERE event_id IS NOT NULL
        """)

        # Replay dedup ledger for non-idempotent writes (dog_metrics increments,
        # mission_log inserts). Rows are pruned on the same schedule as
        # device_events retention.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_robot_events (
                event_id TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
        """)

        # Replay buffer seq counters (persisted across restarts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS replay_seq (
                device_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Retained network_state per device (local-mode contract 2026-07-27).
        # The robot announces its network situation (wifi/ap, ssid, local_ap
        # hotspot credentials) on every relay connect; the app needs the latest
        # one even in sessions that connect AFTER the robot sent it — including
        # after a relay restart while the robot is offline in AP mode, which is
        # exactly the moment the app needs the hotspot info. Hence DB, not RAM.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_network_state (
                device_id TEXT PRIMARY KEY,
                message TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # ---- L-REPORT / Chain STORE scaffold (Data Arch Spec v0.2) --------------
        # Relay-generated per-session natural-language summary. Real schema, per
        # spec section 4. Written by the report-generation layer; read by the app.
        # NOTE: gated behind Edge Workstream A (real training_attempt rows) before
        # it can produce anything — see report_generator.py.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_report (
                report_id     TEXT PRIMARY KEY,        -- UUIDv7, generated on relay
                session_id    TEXT NOT NULL,           -- FK -> session.session_id (edge)
                dog_id        TEXT NOT NULL,            -- denormalized for fast read
                generated_at  INTEGER NOT NULL,        -- epoch ms, UTC
                model_id      TEXT NOT NULL,            -- API model string, e.g. 'claude-haiku-4-5'
                input_hash    TEXT NOT NULL,            -- sha256 of structured input, for idempotency
                summary_text  TEXT NOT NULL,           -- the report shown in the app
                stats_json    TEXT,                    -- the raw numbers the summary was built from
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL
            )
        """)
        # Idempotency guard: same session + same input never produces a duplicate report.
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_session_report_idem
            ON session_report(session_id, input_hash)
        """)

        # Incremental-sync watermark. Verbatim from WIMZ_Data_Architecture_Spec.md
        # section 4 (do not diverge): high-water mark per table so re-sync only
        # pulls newer rows. Spec section 9.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                table_name       TEXT PRIMARY KEY,
                last_synced_at   INTEGER NOT NULL DEFAULT 0,  -- high-water mark (created_at/updated_at)
                last_attempt_at  INTEGER,
                last_error       TEXT
            )
        """)

        # ---- Dec 9 demo build (2026-11-06): per-dog event store + roll-ups ----
        # Robot posts typed events to POST /events; the replayer writes the same
        # table with source='replay'. local_date is the dog-timezone day bucket.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id    TEXT PRIMARY KEY,
                dog_id      TEXT,              -- NULL only for status from a robot with no paired dog
                device_id   TEXT NOT NULL,
                user_id     TEXT,
                type        TEXT NOT NULL,
                ts          TEXT NOT NULL,
                duration_s  REAL,
                data        TEXT NOT NULL DEFAULT '{}',
                source      TEXT NOT NULL DEFAULT 'robot',
                local_date  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        """)
        # Migration (contract fix 2026-11-06): status events are device-scoped, so
        # events.dog_id must accept NULL. SQLite can't drop NOT NULL in place -> rebuild.
        cursor.execute("PRAGMA table_info(events)")
        if any(col[1] == "dog_id" and col[3] for col in cursor.fetchall()):
            backup_db_before_rebuild("events_dog_id_nullable")
            cursor.execute("ALTER TABLE events RENAME TO events_old_notnull")
            cursor.execute("DROP INDEX IF EXISTS idx_events_dog_ts")
            cursor.execute("DROP INDEX IF EXISTS idx_events_dog_date_type")
            cursor.execute("DROP INDEX IF EXISTS idx_events_source")
            cursor.execute("""
                CREATE TABLE events (
                    event_id    TEXT PRIMARY KEY,
                    dog_id      TEXT,
                    device_id   TEXT NOT NULL,
                    user_id     TEXT,
                    type        TEXT NOT NULL,
                    ts          TEXT NOT NULL,
                    duration_s  REAL,
                    data        TEXT NOT NULL DEFAULT '{}',
                    source      TEXT NOT NULL DEFAULT 'robot',
                    local_date  TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
            """)
            cursor.execute("INSERT INTO events SELECT event_id, dog_id, device_id, user_id, type, ts, duration_s, "
                           "data, source, local_date, created_at FROM events_old_notnull")
            cursor.execute("DROP TABLE events_old_notnull")
            logger.info("[MIGRATION] events.dog_id is now nullable (device-scoped status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_dog_ts ON events(dog_id, ts DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_device_type_ts ON events(device_id, type, ts DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_dog_date_type ON events(dog_id, local_date, type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, dog_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                dog_id        TEXT NOT NULL,
                date          TEXT NOT NULL,
                tz            TEXT NOT NULL,
                summary       TEXT NOT NULL,
                event_count   INTEGER NOT NULL,
                last_event_ts TEXT,
                dirty         INTEGER NOT NULL DEFAULT 0,
                computed_at   TEXT NOT NULL,
                PRIMARY KEY (dog_id, date)
            )
        """)
        # Weekly roll-up (2026-11-08): same shape as daily_summary, keyed on the Monday
        # (local) that starts the week; built from that week's daily summaries.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weekly_summary (
                dog_id        TEXT NOT NULL,
                week_start    TEXT NOT NULL,
                tz            TEXT NOT NULL,
                summary       TEXT NOT NULL,
                event_count   INTEGER NOT NULL,
                last_event_ts TEXT,
                dirty         INTEGER NOT NULL DEFAULT 0,
                computed_at   TEXT NOT NULL,
                PRIMARY KEY (dog_id, week_start)
            )
        """)

        # Shared settings (app, MCP connector, voice agent). One JSON value per key.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dog_settings (
                dog_id      TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,
                version     INTEGER NOT NULL DEFAULT 1,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT NOT NULL,
                PRIMARY KEY (dog_id, key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dog_id      TEXT NOT NULL,
                key         TEXT NOT NULL,
                actor       TEXT NOT NULL,
                action      TEXT NOT NULL,
                old_value   TEXT,
                new_value   TEXT,
                reason      TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_settings_audit_dog ON settings_audit(dog_id, created_at DESC)")

        # Notify-rule evaluation log (cooldowns + debugging).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notify_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dog_id      TEXT NOT NULL,
                user_id     TEXT,
                rule_id     TEXT NOT NULL,
                event_id    TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                outcome     TEXT NOT NULL,
                title       TEXT,
                body        TEXT,
                image_url   TEXT,
                clip_url    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notify_log_rule ON notify_log(dog_id, rule_id, outcome, created_at DESC)")

        # Relay-issued treat dispenses (daily-cap accounting until the robot's
        # treat event echoes data.command_id).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS treat_ledger (
                command_id  TEXT PRIMARY KEY,
                dog_id      TEXT NOT NULL,
                actor       TEXT NOT NULL,
                local_date  TEXT NOT NULL,
                issued_at   TEXT NOT NULL,
                event_id    TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_treat_ledger_dog_date ON treat_ledger(dog_id, local_date)")

        # Mission drafts proposed by assistants / the voice agent, approved in the app
        # (amendment 2026-11-06, item 5).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mission_drafts (
                draft_id    TEXT PRIMARY KEY,
                dog_id      TEXT NOT NULL,
                user_id     TEXT,
                actor       TEXT NOT NULL,
                draft       TEXT NOT NULL,
                warnings    TEXT,
                note        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                mission_id  TEXT,
                created_at  TEXT NOT NULL,
                decided_at  TEXT,
                decided_by  TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mission_drafts_dog ON mission_drafts(dog_id, status, created_at DESC)")

        conn.commit()


def get_replay_seqs() -> dict[str, int]:
    """Load all persisted per-device seq counters."""
    with db_connection() as conn:
        rows = conn.execute("SELECT device_id, seq FROM replay_seq").fetchall()
    return {row["device_id"]: row["seq"] for row in rows}


def save_replay_seq(device_id: str, seq: int) -> None:
    """Persist the current seq counter for a device."""
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO replay_seq (device_id, seq) VALUES (?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET seq = excluded.seq",
            (device_id, seq),
        )
        conn.commit()


def save_network_state(device_id: str, message: dict) -> None:
    """Retain the latest network_state message for a device."""
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO device_network_state (device_id, message, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET message = excluded.message, "
            "updated_at = excluded.updated_at",
            (device_id, json.dumps(message), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_network_state(device_id: str) -> Optional[dict]:
    """Return the retained network_state message for a device, or None."""
    with db_connection() as conn:
        row = conn.execute(
            "SELECT message FROM device_network_state WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["message"])
    except (json.JSONDecodeError, TypeError):
        return None


def create_user(user_id: str, email: str, hashed_password: str) -> dict:
    """Create a new user in the database."""
    with db_connection() as conn:
        cursor = conn.cursor()

        created_at = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            "INSERT INTO users (id, email, hashed_password, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, hashed_password, created_at)
        )

        conn.commit()

    return {
        "user_id": user_id,
        "email": email,
        "created_at": created_at
    }


def get_user_by_email(email: str) -> Optional[dict]:
    """Get a user by email address."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()

    if row:
        return {
            "user_id": row["id"],
            "email": row["email"],
            "hashed_password": row["hashed_password"],
            "created_at": row["created_at"]
        }
    return None


def get_user_by_id(user_id: str) -> Optional[dict]:
    """Get a user by ID."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()

    if row:
        return {
            "user_id": row["id"],
            "email": row["email"],
            "hashed_password": row["hashed_password"],
            "created_at": row["created_at"]
        }
    return None


def get_user_count() -> int:
    """Get the total number of users."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]

    return count


def get_next_user_number() -> int:
    """Get the next available user number based on MAX existing ID.

    Avoids collisions that occur when COUNT(*) doesn't match the highest
    allocated user number (e.g. after deletions or manual inserts).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(CAST(SUBSTR(id, 6) AS INTEGER)) FROM users WHERE id LIKE 'user_%'"
        )
        row = cursor.fetchone()
        max_num = row[0] if row[0] is not None else 0
    return max_num + 1


def update_user_name(user_id: str, name: str) -> bool:
    """Update a user's name."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        updated = cursor.rowcount > 0

        conn.commit()

    return updated


# ============== Password Reset Functions ==============

def create_reset_code(email: str, code: str, expires_at: str) -> None:
    """Store a password reset code."""
    with db_connection() as conn:
        cursor = conn.cursor()

        # Invalidate any existing unused codes for this email
        cursor.execute(
            "UPDATE password_reset_codes SET used = 1 WHERE email = ? AND used = 0",
            (email,)
        )

        created_at = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO password_reset_codes (email, code, expires_at, used, created_at) VALUES (?, ?, ?, 0, ?)",
            (email, code, expires_at, created_at)
        )

        conn.commit()


def get_valid_reset_code(email: str, code: str) -> Optional[dict]:
    """Get a valid (unexpired, unused) reset code."""
    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "SELECT * FROM password_reset_codes WHERE email = ? AND code = ? AND used = 0 AND expires_at > ?",
            (email, code, now)
        )
        row = cursor.fetchone()

    if row:
        return {"id": row["id"], "email": row["email"], "code": row["code"]}
    return None


def invalidate_reset_codes(email: str) -> None:
    """Mark all reset codes for an email as used."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE password_reset_codes SET used = 1 WHERE email = ? AND used = 0",
            (email,)
        )

        conn.commit()


def update_user_password(email: str, hashed_password: str) -> bool:
    """Update a user's password by email."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE users SET hashed_password = ? WHERE email = ?",
            (hashed_password, email)
        )
        updated = cursor.rowcount > 0

        conn.commit()

    return updated


# ============== Dog CRUD Functions ==============

def get_dog_count() -> int:
    """Get the total number of dogs."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM dogs")
        count = cursor.fetchone()[0]

    return count


def create_dog(
    dog_id: str,
    name: str,
    user_id: str,
    breed: Optional[str] = None,
    color: Optional[str] = None,
    profile_photo_url: Optional[str] = None,
    aruco_marker_id: Optional[int] = None,
    weight: Optional[float] = None,
    notes: Optional[str] = None,
    goals: Optional[list] = None,
    last_mission_id: Optional[str] = None,
    photo_version: int = 1,
) -> dict:
    """Create a new dog in the database."""
    with db_connection() as conn:
        cursor = conn.cursor()

        created_at = datetime.now(timezone.utc).isoformat()
        updated_at = created_at
        goals_json = json.dumps(goals) if goals else None

        cursor.execute(
            """INSERT INTO dogs (id, name, user_id, breed, color, profile_photo_url, aruco_marker_id,
                                  weight, notes, goals, last_mission_id, photo_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dog_id, name, user_id, breed, color, profile_photo_url, aruco_marker_id,
             weight, notes, goals_json, last_mission_id, photo_version, created_at, updated_at)
        )

        conn.commit()

    return {
        "id": dog_id,
        "name": name,
        "user_id": user_id,
        "breed": breed,
        "color": color,
        "profile_photo_url": profile_photo_url,
        "aruco_marker_id": aruco_marker_id,
        "weight": weight,
        "notes": notes,
        "goals": goals or [],
        "last_mission_id": last_mission_id,
        "photo_version": photo_version,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def check_duplicate_dog_name(user_id: str, name: str) -> bool:
    """Check if a dog with the same name (case-insensitive) already exists for this user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM dogs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, name)
        )
        count = cursor.fetchone()[0]

    return count > 0


def _row_to_dog(row) -> dict:
    """Convert a dogs row to a dict, parsing goals JSON."""
    keys = row.keys()
    goals_raw = row["goals"] if "goals" in keys else None
    try:
        goals = json.loads(goals_raw) if goals_raw else []
    except (json.JSONDecodeError, TypeError):
        goals = []

    return {
        "id": row["id"],
        "name": row["name"],
        "breed": row["breed"],
        "color": row["color"],
        "profile_photo_url": row["profile_photo_url"],
        "aruco_marker_id": row["aruco_marker_id"],
        "visual_features": row["visual_features"] if "visual_features" in keys else None,
        "weight": row["weight"] if "weight" in keys else None,
        "notes": row["notes"] if "notes" in keys else None,
        "goals": goals,
        "last_mission_id": row["last_mission_id"] if "last_mission_id" in keys else None,
        "photo_version": row["photo_version"] if "photo_version" in keys else 1,
        "is_placeholder": bool(row["is_placeholder"]) if "is_placeholder" in keys else False,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] if "updated_at" in keys else row["created_at"],
    }


def get_dog_by_id(dog_id: str) -> Optional[dict]:
    """Get a dog by ID."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,))
        row = cursor.fetchone()

    return _row_to_dog(row) if row else None


def update_dog(dog_id: str, **fields) -> Optional[dict]:
    """Update a dog's fields. JSON-encodes 'goals' if a list is provided. Always bumps updated_at."""
    if not fields:
        return get_dog_by_id(dog_id)

    if "goals" in fields and fields["goals"] is not None and not isinstance(fields["goals"], str):
        fields["goals"] = json.dumps(fields["goals"])

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    if fields.get("name"):
        # an owner naming the robot's placeholder dog makes it a regular dog (same dog_id)
        fields["is_placeholder"] = 0

    with db_connection() as conn:
        cursor = conn.cursor()

        set_clauses = ", ".join([f"{key} = ?" for key in fields.keys()])
        values = list(fields.values()) + [dog_id]

        cursor.execute(f"UPDATE dogs SET {set_clauses} WHERE id = ?", values)
        conn.commit()

    return get_dog_by_id(dog_id)


def delete_dog(dog_id: str, user_id: str = None) -> bool:
    """Delete a dog and its relationships. If user_id is provided, only delete if owned by that user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        # Verify ownership if user_id provided
        if user_id:
            cursor.execute("SELECT id FROM dogs WHERE id = ? AND user_id = ?", (dog_id, user_id))
            if not cursor.fetchone():
                return False

        # Delete user_dogs relationships first
        cursor.execute("DELETE FROM user_dogs WHERE dog_id = ?", (dog_id,))
        # Unpair from robots (a robot left with no dog gets a placeholder on next contact)
        cursor.execute("DELETE FROM device_dogs WHERE dog_id = ?", (dog_id,))
        # Delete dog photos
        cursor.execute("DELETE FROM dog_photos WHERE dog_id = ?", (dog_id,))
        # Delete dog metrics
        cursor.execute("DELETE FROM dog_metrics WHERE dog_id = ?", (dog_id,))
        # Delete mission logs
        cursor.execute("DELETE FROM mission_log WHERE dog_id = ?", (dog_id,))
        # Delete the dog
        cursor.execute("DELETE FROM dogs WHERE id = ?", (dog_id,))
        deleted = cursor.rowcount > 0

        conn.commit()

    return deleted


# ============== User-Dog Relationship Functions ==============

def add_user_dog(user_id: str, dog_id: str, role: str = "owner") -> dict:
    """Add a user-dog relationship."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "INSERT OR REPLACE INTO user_dogs (user_id, dog_id, role) VALUES (?, ?, ?)",
            (user_id, dog_id, role)
        )

        conn.commit()

    return {"user_id": user_id, "dog_id": dog_id, "role": role}


def get_user_dogs(user_id: str) -> list[dict]:
    """Get all dogs for a user with their role, ordered by created_at ascending."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT d.*, ud.role
            FROM dogs d
            JOIN user_dogs ud ON d.id = ud.dog_id
            WHERE ud.user_id = ?
            ORDER BY d.created_at ASC
        """, (user_id,))

        rows = cursor.fetchall()

    result = []
    for row in rows:
        d = _row_to_dog(row)
        d["role"] = row["role"]
        result.append(d)
    return result


def get_user_dog_role(user_id: str, dog_id: str) -> Optional[str]:
    """Get the user's role for a specific dog."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT role FROM user_dogs WHERE user_id = ? AND dog_id = ?",
            (user_id, dog_id)
        )
        row = cursor.fetchone()

    return row["role"] if row else None


def remove_user_dog(user_id: str, dog_id: str) -> bool:
    """Remove a user-dog relationship."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM user_dogs WHERE user_id = ? AND dog_id = ?",
            (user_id, dog_id)
        )
        deleted = cursor.rowcount > 0

        conn.commit()

    return deleted


def _merge_dog_rows(cursor, user_id: str, survivor: str, losers: list[str]) -> dict:
    """Re-key every table that references a loser dog_id onto the survivor, then
    delete the loser dog rows. Runs inside the caller's transaction.

    Unlike the old delete-based cleanup, history is PRESERVED: activity events,
    mission log, schedules, and photos move to the survivor; daily metric rows
    are summed into the survivor's row for the same date.

    Returns {"voice_rekeyed": n, "voice_orphans": m}.
    """
    now = datetime.now(timezone.utc).isoformat()
    stats = {"voice_rekeyed": 0, "voice_orphans": 0}
    for loser in losers:
        # Activity timeline + mission history + schedules follow the survivor.
        cursor.execute("UPDATE activity_events SET dog_id = ? WHERE dog_id = ?", (survivor, loser))
        cursor.execute("UPDATE mission_log SET dog_id = ? WHERE dog_id = ?", (survivor, loser))
        cursor.execute("UPDATE mission_schedules SET dog_id = ? WHERE dog_id = ?", (survivor, loser))

        # Photos move over but never displace the survivor's profile photo.
        cursor.execute(
            "UPDATE dog_photos SET dog_id = ?, is_profile_photo = 0 WHERE dog_id = ?",
            (survivor, loser),
        )

        # Daily metrics: sum loser rows into the survivor's row for the same date.
        cursor.execute("SELECT * FROM dog_metrics WHERE dog_id = ?", (loser,))
        for row in cursor.fetchall():
            cursor.execute(
                """INSERT INTO dog_metrics (dog_id, user_id, date, treat_count, detection_count,
                       mission_attempts, mission_successes, session_minutes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dog_id, date) DO UPDATE SET
                       treat_count = treat_count + excluded.treat_count,
                       detection_count = detection_count + excluded.detection_count,
                       mission_attempts = mission_attempts + excluded.mission_attempts,
                       mission_successes = mission_successes + excluded.mission_successes,
                       session_minutes = session_minutes + excluded.session_minutes,
                       updated_at = excluded.updated_at""",
                (survivor, row["user_id"], row["date"], row["treat_count"], row["detection_count"],
                 row["mission_attempts"], row["mission_successes"], row["session_minutes"],
                 row["created_at"], now),
            )
        cursor.execute("DELETE FROM dog_metrics WHERE dog_id = ?", (loser,))

        # Voice commands re-key onto the survivor; PK collisions mean the
        # survivor already owns that command_id, so leftovers are dropped.
        cursor.execute(
            "UPDATE OR IGNORE voice_commands SET dog_id = ? WHERE dog_id = ?",
            (survivor, loser),
        )
        stats["voice_rekeyed"] += cursor.rowcount
        cursor.execute("DELETE FROM voice_commands WHERE dog_id = ?", (loser,))
        stats["voice_orphans"] += cursor.rowcount

        # User links: move any user's link to the survivor (PK collision = user
        # already linked to the survivor, drop the duplicate link).
        cursor.execute(
            "UPDATE OR IGNORE user_dogs SET dog_id = ? WHERE dog_id = ?",
            (survivor, loser),
        )
        cursor.execute("DELETE FROM user_dogs WHERE dog_id = ?", (loser,))

        # Event store (Dec 9 build) + pairing: events and their audit trails follow the
        # survivor so re-pairing a robot off a placeholder never loses history.
        for table in ("events", "notify_log", "settings_audit", "treat_ledger", "mission_drafts", "mcp_audit"):
            cursor.execute(f"UPDATE {table} SET dog_id = ? WHERE dog_id = ?", (survivor, loser))
        for table in ("session_report", "dog_settings", "device_dogs"):
            # PK/unique collisions: the survivor already has that row and keeps its own
            cursor.execute(f"UPDATE OR IGNORE {table} SET dog_id = ? WHERE dog_id = ?", (survivor, loser))
            cursor.execute(f"DELETE FROM {table} WHERE dog_id = ?", (loser,))
        cursor.execute("DELETE FROM daily_summary WHERE dog_id = ?", (loser,))
        cursor.execute("UPDATE daily_summary SET dirty = 1 WHERE dog_id = ?", (survivor,))
        cursor.execute("DELETE FROM weekly_summary WHERE dog_id = ?", (loser,))
        cursor.execute("UPDATE weekly_summary SET dirty = 1 WHERE dog_id = ?", (survivor,))

        cursor.execute("DELETE FROM dogs WHERE id = ?", (loser,))
    return stats


def merge_dogs(user_id: str, target_dog_id: str, source_dog_ids: list[str]) -> dict:
    """Merge one or more dog rows into a canonical target dog, server-side.

    Used to collapse name variants (e.g. 'Elsa' profiles created across robots
    or test runs) under one canonical dog_id. All referenced history follows the
    target; the source dog rows are deleted.

    Raises ValueError if the target/sources are invalid or not owned by user_id.
    """
    sources = [s for s in dict.fromkeys(source_dog_ids) if s != target_dog_id]
    if not sources:
        raise ValueError("No source dogs to merge (sources empty or same as target)")

    with db_connection() as conn:
        cursor = conn.cursor()

        def _owned(dog_id: str) -> bool:
            cursor.execute(
                """SELECT 1 FROM dogs d
                   LEFT JOIN user_dogs ud ON ud.dog_id = d.id AND ud.user_id = ?
                   WHERE d.id = ? AND (d.user_id = ? OR ud.user_id IS NOT NULL)""",
                (user_id, dog_id, user_id),
            )
            return cursor.fetchone() is not None

        if not _owned(target_dog_id):
            raise ValueError(f"Target dog {target_dog_id} not found or not owned by user")
        for source in sources:
            if not _owned(source):
                raise ValueError(f"Source dog {source} not found or not owned by user")

        stats = _merge_dog_rows(cursor, user_id, target_dog_id, sources)
        conn.commit()

    logger.info(
        f"[DOG-MERGE] user={user_id} target={target_dog_id} merged={sources} "
        f"voice_rekeyed={stats['voice_rekeyed']} voice_orphans={stats['voice_orphans']}"
    )
    return {"target": target_dog_id, "merged": sources, **stats}


def dedupe_duplicate_dogs(dry_run: bool = True) -> dict:
    """One-time / idempotent cleanup of same-name duplicate dog rows per user.

    Relay contract fix 2026-07-12: before POST /dogs honored the client id, a
    forked profile could leave two rows with the same name under one user. This
    collapses each such group to a single survivor:

      - Survivor = the id that voice_commands rows point at (that's the row the
        app/robot actually references); if none or ambiguous, the OLDEST row.
      - Losers   = every other row in the group. All their history (activity
        events, mission log, schedules, photos, metrics, voice commands, user
        links) is re-keyed onto the survivor via _merge_dog_rows; only the loser
        dog rows themselves are deleted.

    Idempotent: with no duplicate names it changes nothing. Always logs the plan;
    with dry_run=True (default) it ONLY logs and returns the plan, touching nothing.
    Returns {"groups": n, "losers": m, "voice_rekeyed": k, "voice_orphans": o}.
    """
    plan: list[dict] = []
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id, user_id, name, created_at FROM dogs WHERE user_id IS NOT NULL")
        rows = cursor.fetchall()

        # Group ids by (user_id, lower(name)), preserving created_at for tie-breaks.
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            key = (r["user_id"], (r["name"] or "").strip().lower())
            groups.setdefault(key, []).append(
                {"id": r["id"], "created_at": r["created_at"] or ""}
            )

        for (user_id, name_lc), members in groups.items():
            if len(members) < 2:
                continue

            member_ids = [m["id"] for m in members]
            # Which of these ids are referenced by voice_commands for this user?
            qmarks = ",".join("?" for _ in member_ids)
            cursor.execute(
                f"SELECT DISTINCT dog_id FROM voice_commands "
                f"WHERE user_id = ? AND dog_id IN ({qmarks})",
                [user_id] + member_ids,
            )
            referenced = {row["dog_id"] for row in cursor.fetchall()}

            members_sorted = sorted(members, key=lambda m: m["created_at"])
            referenced_sorted = [m for m in members_sorted if m["id"] in referenced]
            survivor = (referenced_sorted[0] if referenced_sorted else members_sorted[0])["id"]
            losers = [m["id"] for m in members_sorted if m["id"] != survivor]

            plan.append({
                "user_id": user_id,
                "name": name_lc,
                "survivor": survivor,
                "losers": losers,
                "survivor_from": "voice_ref" if referenced_sorted else "oldest",
            })

        # --- Log the plan (always) ---
        total_losers = sum(len(p["losers"]) for p in plan)
        if not plan:
            logger.info("[DOG-DEDUPE] No same-name duplicate dog groups found (no-op)")
            return {"groups": 0, "losers": 0, "voice_rekeyed": 0, "voice_orphans": 0}

        for p in plan:
            logger.info(
                f"[DOG-DEDUPE]{' DRY-RUN' if dry_run else ''} user={p['user_id']} "
                f"name={p['name']!r} keep={p['survivor']} ({p['survivor_from']}) "
                f"drop={p['losers']}"
            )

        if dry_run:
            logger.info(
                f"[DOG-DEDUPE] DRY-RUN summary: {len(plan)} group(s), "
                f"{total_losers} loser row(s) would be removed"
            )
            return {"groups": len(plan), "losers": total_losers,
                    "voice_rekeyed": 0, "voice_orphans": 0}

        # --- Apply: full merge (history re-keyed onto the survivor, not deleted) ---
        rekeyed = 0
        orphans = 0
        for p in plan:
            stats = _merge_dog_rows(cursor, p["user_id"], p["survivor"], p["losers"])
            rekeyed += stats["voice_rekeyed"]
            orphans += stats["voice_orphans"]

        conn.commit()

    logger.info(
        f"[DOG-DEDUPE] Applied: {len(plan)} group(s), {total_losers} loser row(s) merged, "
        f"{rekeyed} voice row(s) re-keyed, {orphans} voice orphan(s) dropped"
    )
    return {"groups": len(plan), "losers": total_losers,
            "voice_rekeyed": rekeyed, "voice_orphans": orphans}


# ============== Dog Photo Functions ==============

def get_photo_count() -> int:
    """Get the total number of dog photos."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM dog_photos")
        count = cursor.fetchone()[0]

    return count


def create_dog_photo(
    photo_id: str,
    dog_id: str,
    photo_url: str,
    is_profile_photo: bool = False
) -> dict:
    """Create a new dog photo."""
    with db_connection() as conn:
        cursor = conn.cursor()

        captured_at = datetime.now(timezone.utc).isoformat()

        # If this is a profile photo, unset any existing profile photo
        if is_profile_photo:
            cursor.execute(
                "UPDATE dog_photos SET is_profile_photo = 0 WHERE dog_id = ?",
                (dog_id,)
            )
            # Also update the dog's profile_photo_url
            cursor.execute(
                "UPDATE dogs SET profile_photo_url = ? WHERE id = ?",
                (photo_url, dog_id)
            )

        cursor.execute(
            """INSERT INTO dog_photos (id, dog_id, photo_url, is_profile_photo, captured_at)
               VALUES (?, ?, ?, ?, ?)""",
            (photo_id, dog_id, photo_url, 1 if is_profile_photo else 0, captured_at)
        )

        conn.commit()

    return {
        "id": photo_id,
        "dog_id": dog_id,
        "photo_url": photo_url,
        "is_profile_photo": is_profile_photo,
        "captured_at": captured_at
    }


def get_dog_photos(dog_id: str) -> list[dict]:
    """Get all photos for a dog."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM dog_photos WHERE dog_id = ? ORDER BY captured_at DESC",
            (dog_id,)
        )
        rows = cursor.fetchall()

    return [
        {
            "id": row["id"],
            "dog_id": row["dog_id"],
            "photo_url": row["photo_url"],
            "is_profile_photo": bool(row["is_profile_photo"]),
            "captured_at": row["captured_at"]
        }
        for row in rows
    ]


# ============== Device Pairing Functions ==============

def create_device_pairing(user_id: str, device_id: str) -> dict:
    """Create or update a device pairing."""
    with db_connection() as conn:
        cursor = conn.cursor()

        paired_at = datetime.now(timezone.utc).isoformat()

        # Check if device exists in robots table
        cursor.execute("SELECT id FROM robots WHERE device_id = ?", (device_id,))
        robot = cursor.fetchone()

        if robot:
            # Update existing robot's owner
            cursor.execute(
                "UPDATE robots SET owner_user_id = ? WHERE device_id = ?",
                (user_id, device_id)
            )
        else:
            # Create new robot entry
            robot_id = f"robot_{device_id}"
            cursor.execute(
                """INSERT INTO robots (id, device_id, owner_user_id, name, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (robot_id, device_id, user_id, f"WIM-Z {device_id[-6:]}", paired_at)
            )

        conn.commit()

    logger.info(f"[PAIRING] Created: device {device_id} -> user {user_id}")
    return {"user_id": user_id, "device_id": device_id, "paired_at": paired_at}


def delete_device_pairing(device_id: str) -> bool:
    """Remove device pairing (set owner to NULL)."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE robots SET owner_user_id = NULL WHERE device_id = ?",
            (device_id,)
        )
        deleted = cursor.rowcount > 0

        conn.commit()

    if deleted:
        logger.info(f"[PAIRING] Deleted: device {device_id} unpaired")
    return deleted


def get_device_owner(device_id: str) -> Optional[str]:
    """Get the owner user_id for a device."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT owner_user_id FROM robots WHERE device_id = ?",
            (device_id,)
        )
        row = cursor.fetchone()

    return row["owner_user_id"] if row else None


def get_device_secret(device_id: str) -> Optional[str]:
    """Get the per-device HMAC secret from the robots table, if set."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT device_secret FROM robots WHERE device_id = ?",
            (device_id,)
        )
        row = cursor.fetchone()

    return row["device_secret"] if row and row["device_secret"] else None


def get_all_device_pairings() -> dict[str, str]:
    """Get all device_id -> user_id pairings for loading into memory."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT device_id, owner_user_id FROM robots WHERE owner_user_id IS NOT NULL"
        )
        rows = cursor.fetchall()

    return {row["device_id"]: row["owner_user_id"] for row in rows}


def get_user_paired_devices(user_id: str) -> list[str]:
    """Get all device_ids paired with a user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT device_id FROM robots WHERE owner_user_id = ?",
            (user_id,)
        )
        rows = cursor.fetchall()

    return [row["device_id"] for row in rows]


def seed_default_pairings():
    """Seed default pairings for testing if they don't exist."""
    with db_connection() as conn:
        cursor = conn.cursor()

        # Check if we already have pairings
        cursor.execute("SELECT COUNT(*) FROM robots WHERE owner_user_id IS NOT NULL")
        if cursor.fetchone()[0] > 0:
            logger.debug("[PAIRING] Seed skipped - pairings already exist")
            return  # Already seeded

        now = datetime.now(timezone.utc).isoformat()

        # Seed default test pairings: (id, device_id, owner_user_id, name, device_secret)
        default_pairings = [
            ("robot_001", "wimz_robot_01", "user_000003", "WIM-Z Robot 01", None),
            ("robot_002", "wimz_robot_02", "user_000003", "WIM-Z Robot 02", None),
            ("robot_003", "wimz_robot_03", "user_000003", "WIM-Z Robot 03", "hwPpwQG6bIXNIQYOK5sdHCkb9weY64wzbnBzr8f7lWY"),
            ("robot_004", "wimz_robot_04", "user_000003", "WIM-Z Robot 04", "OsRl_fMmesv-8CYskeLran9pBWUato-2kXQF_jVTuMk"),
            ("robot_005", "wimz_robot_05", "user_000003", "WIM-Z Robot 05", "tP1KfyVSVykEkOWkgzxFja9KITznc47tsi4njnVYfDg"),
        ]

        for robot_id, device_id, owner_id, name, secret in default_pairings:
            cursor.execute(
                """INSERT OR IGNORE INTO robots (id, device_id, owner_user_id, name, device_secret, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (robot_id, device_id, owner_id, name, secret, now)
            )
            logger.info(f"[PAIRING] Seeded: device {device_id} -> user {owner_id}")

        conn.commit()


# ============== Dog Metrics Functions ==============

def log_metric(dog_id: str, user_id: str, metric_type: str, value: int = 1) -> dict:
    """Upsert a daily metric row. metric_type must be one of: treat_count, detection_count, session_minutes."""
    valid_metrics = ("treat_count", "detection_count", "mission_attempts", "mission_successes", "session_minutes")
    if metric_type not in valid_metrics:
        raise ValueError(f"Invalid metric_type: {metric_type}. Must be one of {valid_metrics}")

    with db_connection() as conn:
        cursor = conn.cursor()

        today = date.today().isoformat()
        now = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            f"""INSERT INTO dog_metrics (dog_id, user_id, date, {metric_type}, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dog_id, date) DO UPDATE SET
                    {metric_type} = {metric_type} + ?,
                    updated_at = ?""",
            (dog_id, user_id, today, value, now, now, value, now)
        )

        conn.commit()

    logger.info(f"[METRICS] Logged {metric_type}+={value} for dog {dog_id}")
    return {"dog_id": dog_id, "metric_type": metric_type, "value": value, "date": today}


def log_mission(dog_id: str, user_id: str, mission_type: str, result: str, details: str = None) -> dict:
    """Log a mission event and update daily metrics."""
    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        today = date.today().isoformat()

        # Insert mission log entry
        cursor.execute(
            """INSERT INTO mission_log (dog_id, user_id, mission_type, result, details, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (dog_id, user_id, mission_type, result, details, now)
        )

        # Upsert daily metrics: always increment attempts, increment successes if result is "success"
        success_increment = 1 if result == "success" else 0
        cursor.execute(
            """INSERT INTO dog_metrics (dog_id, user_id, date, mission_attempts, mission_successes, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(dog_id, date) DO UPDATE SET
                   mission_attempts = mission_attempts + 1,
                   mission_successes = mission_successes + ?,
                   updated_at = ?""",
            (dog_id, user_id, today, success_increment, now, now, success_increment, now)
        )

        conn.commit()

    logger.info(f"[METRICS] Mission logged: dog={dog_id}, type={mission_type}, result={result}")
    return {"dog_id": dog_id, "mission_type": mission_type, "result": result, "timestamp": now}


def get_metrics(dog_id: str, user_id: str, since_date: str = None) -> dict:
    """Get aggregated metrics for a dog since a given date. Returns summed totals."""
    with db_connection() as conn:
        cursor = conn.cursor()

        if since_date:
            cursor.execute(
                """SELECT
                    COALESCE(SUM(treat_count), 0) as treat_count,
                    COALESCE(SUM(detection_count), 0) as detection_count,
                    COALESCE(SUM(mission_attempts), 0) as mission_attempts,
                    COALESCE(SUM(mission_successes), 0) as mission_successes,
                    COALESCE(SUM(session_minutes), 0) as session_minutes
                FROM dog_metrics
                WHERE dog_id = ? AND user_id = ? AND date >= ?""",
                (dog_id, user_id, since_date)
            )
        else:
            cursor.execute(
                """SELECT
                    COALESCE(SUM(treat_count), 0) as treat_count,
                    COALESCE(SUM(detection_count), 0) as detection_count,
                    COALESCE(SUM(mission_attempts), 0) as mission_attempts,
                    COALESCE(SUM(mission_successes), 0) as mission_successes,
                    COALESCE(SUM(session_minutes), 0) as session_minutes
                FROM dog_metrics
                WHERE dog_id = ? AND user_id = ?""",
                (dog_id, user_id)
            )

        row = cursor.fetchone()

    return {
        "dog_id": dog_id,
        "treat_count": row["treat_count"],
        "detection_count": row["detection_count"],
        "mission_attempts": row["mission_attempts"],
        "mission_successes": row["mission_successes"],
        "session_minutes": round(row["session_minutes"], 1),
    }


def get_metric_history(dog_id: str, user_id: str, days: int = 7) -> list[dict]:
    """Get daily metric breakdown for the last N days."""
    with db_connection() as conn:
        cursor = conn.cursor()

        since_date = (date.today() - timedelta(days=days)).isoformat()

        cursor.execute(
            """SELECT date, treat_count, detection_count, mission_attempts, mission_successes, session_minutes
               FROM dog_metrics
               WHERE dog_id = ? AND user_id = ? AND date >= ?
               ORDER BY date ASC""",
            (dog_id, user_id, since_date)
        )

        rows = cursor.fetchall()

    return [
        {
            "date": row["date"],
            "treat_count": row["treat_count"],
            "detection_count": row["detection_count"],
            "mission_attempts": row["mission_attempts"],
            "mission_successes": row["mission_successes"],
            "session_minutes": round(row["session_minutes"], 1),
        }
        for row in rows
    ]


# ============== Mission Schedule Functions (Build 34) ==============

def create_schedule(
    schedule_id: str,
    user_id: str,
    dog_id: str,
    mission_id: str,
    schedule_type: str = "daily",
    hour: int = 9,
    minute: int = 0,
    end_hour: int = None,
    end_minute: int = None,
    weekdays: list[int] = None,
    cooldown_hours: int = None,
    name: str = None,
    enabled: bool = True
) -> dict:
    """Create a new mission schedule."""
    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        weekdays_str = ",".join(map(str, weekdays)) if weekdays else None

        cursor.execute(
            """INSERT INTO mission_schedules
               (id, user_id, dog_id, mission_id, name, type, hour, minute, end_hour, end_minute, weekdays, cooldown_hours, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (schedule_id, user_id, dog_id, mission_id, name, schedule_type, hour, minute, end_hour, end_minute, weekdays_str, cooldown_hours, 1 if enabled else 0, now, now)
        )

        conn.commit()

    logger.info(f"[SCHEDULE] Created: {schedule_id} for user {user_id}, mission {mission_id}")
    return get_schedule_by_id(schedule_id)


def get_schedule_by_id(schedule_id: str) -> Optional[dict]:
    """Get a schedule by ID."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM mission_schedules WHERE id = ?", (schedule_id,))
        row = cursor.fetchone()

    if row:
        return _row_to_schedule(row)
    return None


def get_user_schedules(user_id: str) -> list[dict]:
    """Get all schedules for a user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM mission_schedules WHERE user_id = ? ORDER BY hour, minute",
            (user_id,)
        )
        rows = cursor.fetchall()

    return [_row_to_schedule(row) for row in rows]


def update_schedule(schedule_id: str, user_id: str, **fields) -> Optional[dict]:
    """Update a schedule. Returns None if not found or not owned by user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        # Verify ownership
        cursor.execute("SELECT id FROM mission_schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id))
        if not cursor.fetchone():
            return None

        # Handle weekdays conversion
        if "weekdays" in fields and fields["weekdays"] is not None:
            fields["weekdays"] = ",".join(map(str, fields["weekdays"]))

        # Handle enabled conversion
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0

        # Rename 'type' field if present (reserved word)
        if "type" in fields:
            fields["type"] = fields.pop("type")

        if not fields:
            return get_schedule_by_id(schedule_id)

        now = datetime.now(timezone.utc).isoformat()
        fields["updated_at"] = now

        set_clauses = ", ".join([f"{key} = ?" for key in fields.keys()])
        values = list(fields.values()) + [schedule_id]

        cursor.execute(f"UPDATE mission_schedules SET {set_clauses} WHERE id = ?", values)
        conn.commit()

    logger.info(f"[SCHEDULE] Updated: {schedule_id}")
    return get_schedule_by_id(schedule_id)


def delete_schedule(schedule_id: str, user_id: str) -> bool:
    """Delete a schedule. Returns False if not found or not owned by user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM mission_schedules WHERE id = ? AND user_id = ?",
            (schedule_id, user_id)
        )
        deleted = cursor.rowcount > 0

        conn.commit()

    if deleted:
        logger.info(f"[SCHEDULE] Deleted: {schedule_id}")
    return deleted


NUM_TO_DAY_NAME = {
    0: "sunday", 1: "monday", 2: "tuesday", 3: "wednesday",
    4: "thursday", 5: "friday", 6: "saturday"
}


def _row_to_schedule(row) -> dict:
    """Convert a database row to a schedule dict with both relay and app formats."""
    weekdays = None
    days_of_week = None
    if row["weekdays"]:
        weekdays = [int(d) for d in row["weekdays"].split(",")]
        days_of_week = [NUM_TO_DAY_NAME.get(d, "") for d in weekdays]

    # Format times as "HH:MM" for app compatibility
    start_time = f"{row['hour']:02d}:{row['minute']:02d}"
    end_time = None
    end_hour = row["end_hour"] if "end_hour" in row.keys() else None
    end_minute = row["end_minute"] if "end_minute" in row.keys() else None
    if end_hour is not None and end_minute is not None:
        end_time = f"{end_hour:02d}:{end_minute:02d}"

    cooldown_hours = row["cooldown_hours"] if "cooldown_hours" in row.keys() else None

    return {
        # Both formats for ID
        "id": row["id"],
        "schedule_id": row["id"],  # App compatibility
        "user_id": row["user_id"],
        "dog_id": row["dog_id"],
        # Both formats for mission
        "mission_id": row["mission_id"],
        "mission_name": row["mission_id"],  # App compatibility
        "name": row["name"],
        "type": row["type"],
        # Both formats for time
        "hour": row["hour"],
        "minute": row["minute"],
        "start_time": start_time,  # App compatibility
        "end_time": end_time,  # App compatibility
        # Both formats for weekdays
        "weekdays": weekdays,
        "days_of_week": days_of_week,  # App compatibility
        "cooldown_hours": cooldown_hours,  # App compatibility
        "enabled": bool(row["enabled"]),
        "next_run": row["next_run"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ============== User Settings Functions (Build 34) ==============

def get_scheduling_enabled(user_id: str) -> bool:
    """Check if scheduling is enabled for a user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT scheduling_enabled FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()

    # Default to enabled if no setting exists
    return bool(row["scheduling_enabled"]) if row else True


def set_scheduling_enabled(user_id: str, enabled: bool) -> bool:
    """Enable or disable scheduling for a user."""
    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """INSERT INTO user_settings (user_id, scheduling_enabled, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   scheduling_enabled = ?,
                   updated_at = ?""",
            (user_id, 1 if enabled else 0, now, 1 if enabled else 0, now)
        )

        conn.commit()

    logger.info(f"[SCHEDULE] User {user_id} scheduling {'enabled' if enabled else 'disabled'}")
    return True


# ============== Device Event Storage ==============

STORABLE_EVENT_TYPES = {
    "mission_progress", "mission_complete", "mission_stopped",
    "mode_changed", "dog_detected", "treat_dispensed", "bark_detected", "bark",
    "upload_complete", "upload_error", "upload_result",
    "audio_state",
    "schedule_created", "schedule_updated", "schedule_deleted", "schedule_triggered",
    "guardian",
    # Silent Guardian summaries + panic alerts (robot contract 2026-08-31).
    "sg_summary",
    "panic_alert",
    "error",
}


def store_event(device_id: str, owner_user_id: str, event_type: str, message: dict) -> Optional[int]:
    """Store a robot event for offline retrieval. Returns row id, or None if not
    storable or a replay of an already-stored event (deduped on event_id)."""
    if event_type not in STORABLE_EVENT_TYPES:
        return None

    # Live query responses (sg_summary action="status_pull") are forwarded but
    # never stored — see event_buffer.is_durable_event.
    from app.event_buffer import is_durable_event
    if not is_durable_event(event_type, message):
        return None

    event_id = message.get("event_id") or message.get("id")
    event_id = str(event_id) if event_id else None

    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        timestamp = message.get("timestamp", now)
        message_json = json.dumps(message)

        cursor.execute(
            """INSERT OR IGNORE INTO device_events
               (device_id, owner_user_id, event_type, message, timestamp, created_at, event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (device_id, owner_user_id, event_type, message_json, timestamp, now, event_id)
        )

        if cursor.rowcount == 0:
            conn.commit()
            logger.info(f"[EVENT-STORE] Deduped replayed {event_type} for device {device_id} (event_id={event_id})")
            return None

        row_id = cursor.lastrowid
        conn.commit()

    logger.debug(f"[EVENT-STORE] Stored {event_type} for device {device_id} (id={row_id})")
    return row_id


def mark_robot_event_processed(event_id: str) -> bool:
    """Record a robot event id in the replay-dedup ledger.

    Returns True if this is the first time the id is seen (caller should apply
    its non-idempotent side effects), False if it was already processed (replay
    — caller must skip persistence to avoid double-counting).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO processed_robot_events (event_id, seen_at) VALUES (?, ?)",
            (event_id, datetime.now(timezone.utc).isoformat()),
        )
        first_time = cursor.rowcount > 0
        conn.commit()
    return first_time


def delete_old_processed_events(days: int = 30) -> int:
    """Prune replay-dedup ledger entries older than N days. Returns count deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor.execute("DELETE FROM processed_robot_events WHERE seen_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
    if deleted > 0:
        logger.info(f"[EVENT-CLEANUP] Pruned {deleted} processed-event ledger rows older than {days} days")
    return deleted


def get_device_events(
    device_id: str,
    owner_user_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    event_type: Optional[str] = None,
    dog_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Retrieve stored events with pagination and optional filters."""
    with db_connection() as conn:
        cursor = conn.cursor()

        conditions = ["device_id = ?", "owner_user_id = ?"]
        params: list = [device_id, owner_user_id]

        if start:
            conditions.append("timestamp >= ?")
            params.append(start)
        if end:
            conditions.append("timestamp <= ?")
            params.append(end)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if dog_id:
            # dog_id may be at top level or inside data object
            conditions.append(
                "(json_extract(message, '$.dog_id') = ? OR json_extract(message, '$.data.dog_id') = ?)"
            )
            params.extend([dog_id, dog_id])

        where_clause = " AND ".join(conditions)

        cursor.execute(f"SELECT COUNT(*) FROM device_events WHERE {where_clause}", params)
        total = cursor.fetchone()[0]

        cursor.execute(
            f"""SELECT id, device_id, event_type, message, timestamp
                FROM device_events
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset]
        )

        rows = cursor.fetchall()

    events = []
    for row in rows:
        try:
            msg = json.loads(row["message"])
        except (json.JSONDecodeError, TypeError):
            msg = {}
        events.append({
            "id": row["id"],
            "device_id": row["device_id"],
            "event_type": row["event_type"],
            "message": msg,
            "timestamp": row["timestamp"],
        })

    return {
        "events": events,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_device_event_summary(device_id: str, owner_user_id: str, days: int = 7) -> dict:
    """Aggregate event counts over a period for the dashboard summary."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Daily breakdown by event type
        cursor.execute(
            """SELECT date(timestamp) as day, event_type, COUNT(*) as cnt
               FROM device_events
               WHERE device_id = ? AND owner_user_id = ? AND timestamp >= ?
               GROUP BY day, event_type
               ORDER BY day""",
            (device_id, owner_user_id, cutoff)
        )
        rows = cursor.fetchall()

    # Accumulate per-day and totals
    daily: dict[str, dict] = {}
    total_treats = 0
    total_tricks = 0
    total_barks = 0
    total_events = 0

    for row in rows:
        day = row["day"]
        etype = row["event_type"]
        cnt = row["cnt"]
        total_events += cnt

        if day not in daily:
            daily[day] = {"treats": 0, "tricks": 0, "barks": 0, "detections": 0, "events": 0}

        daily[day]["events"] += cnt

        if etype == "treat_dispensed":
            daily[day]["treats"] += cnt
            total_treats += cnt
        elif etype in ("mission_complete", "mission_stopped"):
            daily[day]["tricks"] += cnt
            total_tricks += cnt
        elif etype == "bark_detected":
            daily[day]["barks"] += cnt
            total_barks += cnt
        elif etype == "dog_detected":
            daily[day]["detections"] += cnt

    # Compute daily scores: weighted composite (treats=3, tricks=5, detections=1, cap 100)
    daily_scores = []
    for day in sorted(daily.keys()):
        d = daily[day]
        raw = d["treats"] * 3 + d["tricks"] * 5 + d["detections"] * 1
        score = min(raw, 100)
        daily_scores.append({"date": day, "score": score})

    # Estimate active minutes from event density (each event ~1 min of activity)
    active_minutes = total_events

    return {
        "daily_scores": daily_scores,
        "total_treats": total_treats,
        "total_tricks": total_tricks,
        "total_barks": total_barks,
        "active_minutes": active_minutes,
        "period_days": days,
    }


# ============== Voice Commands (Phase 2 / A2) ==============

def upsert_voice_command(
    user_id: str,
    dog_id: str,
    command_id: str,
    file_path: str,
    format: str,
    size_bytes: int,
) -> dict:
    """Insert or update a voice command record. Returns the persisted row."""
    with db_connection() as conn:
        cursor = conn.cursor()
        updated_at = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """INSERT INTO voice_commands (user_id, dog_id, command_id, file_path, format, size_bytes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, dog_id, command_id) DO UPDATE SET
                   file_path = excluded.file_path,
                   format = excluded.format,
                   size_bytes = excluded.size_bytes,
                   updated_at = excluded.updated_at""",
            (user_id, dog_id, command_id, file_path, format, size_bytes, updated_at),
        )
        conn.commit()

    return {
        "user_id": user_id,
        "dog_id": dog_id,
        "command_id": command_id,
        "file_path": file_path,
        "format": format,
        "size_bytes": size_bytes,
        "updated_at": updated_at,
    }


def list_voice_commands(user_id: str, dog_id: str) -> list[dict]:
    """List all voice commands for a (user_id, dog_id) pair."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT user_id, dog_id, command_id, file_path, format, size_bytes, updated_at
               FROM voice_commands
               WHERE user_id = ? AND dog_id = ?
               ORDER BY updated_at DESC""",
            (user_id, dog_id),
        )
        rows = cursor.fetchall()

    return [
        {
            "user_id": row["user_id"],
            "dog_id": row["dog_id"],
            "command_id": row["command_id"],
            "file_path": row["file_path"],
            "format": row["format"],
            "size_bytes": row["size_bytes"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def list_voice_commands_for_user(user_id: str) -> list[dict]:
    """List all voice commands across all of a user's dogs (robot connect replay)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT user_id, dog_id, command_id, file_path, format, size_bytes, updated_at
               FROM voice_commands
               WHERE user_id = ?
               ORDER BY dog_id, command_id""",
            (user_id,),
        )
        rows = cursor.fetchall()

    return [
        {
            "user_id": row["user_id"],
            "dog_id": row["dog_id"],
            "command_id": row["command_id"],
            "file_path": row["file_path"],
            "format": row["format"],
            "size_bytes": row["size_bytes"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_voice_command(user_id: str, dog_id: str, command_id: str) -> Optional[dict]:
    """Fetch a single voice command record."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT user_id, dog_id, command_id, file_path, format, size_bytes, updated_at
               FROM voice_commands
               WHERE user_id = ? AND dog_id = ? AND command_id = ?""",
            (user_id, dog_id, command_id),
        )
        row = cursor.fetchone()

    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "dog_id": row["dog_id"],
        "command_id": row["command_id"],
        "file_path": row["file_path"],
        "format": row["format"],
        "size_bytes": row["size_bytes"],
        "updated_at": row["updated_at"],
    }


def delete_voice_command(user_id: str, dog_id: str, command_id: str) -> Optional[dict]:
    """Delete the row and return the previous state, or None if it didn't exist."""
    existing = get_voice_command(user_id, dog_id, command_id)
    if not existing:
        return None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM voice_commands WHERE user_id = ? AND dog_id = ? AND command_id = ?",
            (user_id, dog_id, command_id),
        )
        conn.commit()
    return existing


def delete_old_events(days: int = 30) -> int:
    """Delete events older than N days. Returns count deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor.execute("DELETE FROM device_events WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount

        conn.commit()

    if deleted > 0:
        logger.info(f"[EVENT-CLEANUP] Deleted {deleted} events older than {days} days")
    return deleted


# ============== Activity Events (Phase 3 / A3) ==============

ACTIVITY_RETENTION_DAYS = 90


def upsert_push_token(
    device_token: str,
    user_id: str,
    platform: str,
    enabled_types: list[str],
) -> dict:
    """Insert or update an FCM push token row (keyed by device_token).

    Serves first registration, FCM token rotation, and preference edits alike —
    last write wins. An empty enabled_types list keeps the row but sends nothing.
    """
    updated_at = datetime.now(timezone.utc).isoformat()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO push_tokens (device_token, user_id, platform, enabled_types, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(device_token) DO UPDATE SET
                   user_id = excluded.user_id,
                   platform = excluded.platform,
                   enabled_types = excluded.enabled_types,
                   updated_at = excluded.updated_at""",
            (device_token, user_id, platform, json.dumps(enabled_types), updated_at),
        )
        conn.commit()
    return {
        "device_token": device_token,
        "user_id": user_id,
        "platform": platform,
        "enabled_types": enabled_types,
        "updated_at": updated_at,
    }


def delete_push_token(device_token: str) -> bool:
    """Remove a push token row (logout, or FCM reported it invalid)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM push_tokens WHERE device_token = ?", (device_token,))
        deleted = cursor.rowcount > 0
        conn.commit()
    return deleted


def list_push_tokens_for_user(user_id: str) -> list[dict]:
    """All registered push devices for a user, enabled_types decoded."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT device_token, user_id, platform, enabled_types, updated_at
               FROM push_tokens WHERE user_id = ?""",
            (user_id,),
        )
        rows = cursor.fetchall()

    result = []
    for row in rows:
        try:
            enabled = json.loads(row["enabled_types"])
        except (TypeError, ValueError):
            enabled = []
        result.append({
            "device_token": row["device_token"],
            "user_id": row["user_id"],
            "platform": row["platform"],
            "enabled_types": enabled if isinstance(enabled, list) else [],
            "updated_at": row["updated_at"],
        })
    return result


def insert_activity_event(
    event_id: str,
    user_id: str,
    device_id: str,
    dog_id: Optional[str],
    type: str,
    timestamp: str,
    payload: Optional[dict],
) -> dict:
    """Persist a single activity event row."""
    with db_connection() as conn:
        cursor = conn.cursor()
        created_at = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload) if payload is not None else None
        cursor.execute(
            """INSERT OR IGNORE INTO activity_events (id, user_id, device_id, dog_id, type, timestamp, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, user_id, device_id, dog_id, type, timestamp, payload_json, created_at),
        )
        inserted = cursor.rowcount > 0
        conn.commit()

    return {
        "id": event_id,
        "user_id": user_id,
        "device_id": device_id,
        "dog_id": dog_id,
        "type": type,
        "timestamp": timestamp,
        "payload": payload or {},
        "created_at": created_at,
        # False = the id already existed (replayed robot event). Push sending
        # keys off this so a replay never re-notifies.
        "inserted": inserted,
    }


def query_activity_events(
    user_id: str,
    dog_id: Optional[str] = None,
    since: Optional[str] = None,
    cursor_ts: Optional[str] = None,
    cursor_id: Optional[str] = None,
    limit: int = 100,
    include_cleared: bool = False,
) -> list[dict]:
    """Query events, sorted by (timestamp DESC, id DESC). Returns up to `limit` rows.

    `cursor_ts` + `cursor_id` form the keyset cursor: rows strictly older than the cursor.

    Cleared events (cleared_at IS NOT NULL) are excluded by default so a durable
    clear does not reappear on the timeline. Pass `include_cleared=True` to see them.
    """
    with db_connection() as conn:
        cursor = conn.cursor()

        conditions = ["user_id = ?"]
        params: list = [user_id]

        if not include_cleared:
            conditions.append("cleared_at IS NULL")

        if dog_id is not None:
            conditions.append("dog_id = ?")
            params.append(dog_id)

        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)

        if cursor_ts is not None:
            # Strictly older than cursor (keyset pagination by (timestamp, id))
            conditions.append("(timestamp < ? OR (timestamp = ? AND id < ?))")
            params.extend([cursor_ts, cursor_ts, cursor_id or ""])

        where_clause = " AND ".join(conditions)

        cursor.execute(
            f"""SELECT id, user_id, device_id, dog_id, type, timestamp, payload, created_at
                FROM activity_events
                WHERE {where_clause}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?""",
            params + [limit],
        )
        rows = cursor.fetchall()

    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        result.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "device_id": row["device_id"],
            "dog_id": row["dog_id"],
            "type": row["type"],
            "timestamp": row["timestamp"],
            "payload": payload,
            "created_at": row["created_at"],
        })
    return result


def clear_activity_events(
    user_id: str,
    event_ids: Optional[list[str]] = None,
    dog_id: Optional[str] = None,
    before_ts: Optional[str] = None,
) -> dict:
    """Durably mark activity events as cleared (soft-clear, never delete).

    L-SYNC / Chain SG. This is the write-back that makes a clear survive re-query,
    fixing the "cleared events reappear" bug. Rows are kept for history/moat; they
    just leave the timeline (see query_activity_events).

    Selectors (combinable), always scoped to `user_id`:
      - event_ids: clear exactly these rows (per-event dismiss).
      - dog_id:    restrict clear-all to a single dog.
      - before_ts: restrict clear-all to events at/older than this ISO timestamp.
    With no selectors, clears ALL of the user's events (full clear-all).

    Idempotent: only rows with cleared_at IS NULL are touched, so re-issuing a
    clear is a no-op and an event re-sent by the robot (INSERT OR IGNORE) keeps
    its cleared_at. Returns {cleared, event_ids, device_ids} for the robot round-trip.
    """
    now = datetime.now(timezone.utc).isoformat()

    conditions = ["user_id = ?", "cleared_at IS NULL"]
    params: list = [user_id]

    if event_ids:
        placeholders = ",".join("?" for _ in event_ids)
        conditions.append(f"id IN ({placeholders})")
        params.extend(event_ids)
    if dog_id is not None:
        conditions.append("dog_id = ?")
        params.append(dog_id)
    if before_ts is not None:
        conditions.append("timestamp <= ?")
        params.append(before_ts)

    where_clause = " AND ".join(conditions)

    with db_connection() as conn:
        cursor = conn.cursor()

        # Capture the exact set being cleared, so the caller can round-trip the
        # clear to the owning robot(s) and confirm ids back to the app.
        cursor.execute(
            f"SELECT id, device_id FROM activity_events WHERE {where_clause}",
            params,
        )
        affected = cursor.fetchall()
        cleared_ids = [row["id"] for row in affected]
        device_ids = sorted({row["device_id"] for row in affected if row["device_id"]})

        if cleared_ids:
            cursor.execute(
                f"UPDATE activity_events SET cleared_at = ? WHERE {where_clause}",
                [now] + params,
            )
            conn.commit()

    logger.info(
        f"[ACTIVITY-CLEAR] user={user_id} cleared={len(cleared_ids)} "
        f"devices={device_ids} scope="
        f"{'ids' if event_ids else 'all'}{'+dog' if dog_id else ''}{'+before' if before_ts else ''}"
    )
    return {"cleared": len(cleared_ids), "event_ids": cleared_ids, "device_ids": device_ids}


def delete_old_activity_events(days: int = ACTIVITY_RETENTION_DAYS) -> int:
    """Apply rolling retention to activity_events. Returns count deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor.execute("DELETE FROM activity_events WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()

    if deleted > 0:
        logger.info(f"[ACTIVITY-CLEANUP] Deleted {deleted} events older than {days} days")
    return deleted


# ============== Session Reports (L-REPORT / Chain STORE, spec v0.2) ==============

def get_session_report(session_id: str, input_hash: str) -> Optional[dict]:
    """Return an existing report for (session_id, input_hash), or None.

    This is the idempotency check: if a report already exists for the same
    session and the same structured input, the generator returns it instead of
    making a second API call.
    """
    with db_connection() as conn:
        row = conn.execute(
            """SELECT report_id, session_id, dog_id, generated_at, model_id,
                      input_hash, summary_text, stats_json, created_at, updated_at
               FROM session_report
               WHERE session_id = ? AND input_hash = ?""",
            (session_id, input_hash),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    if result.get("stats_json"):
        try:
            result["stats_json"] = json.loads(result["stats_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def upsert_session_report(
    report_id: str,
    session_id: str,
    dog_id: str,
    generated_at: int,
    model_id: str,
    input_hash: str,
    summary_text: str,
    stats_json: Optional[dict],
) -> dict:
    """Persist a generated session report. Idempotent on (session_id, input_hash).

    On conflict the existing row wins (a re-run with identical input is a no-op),
    keeping generation idempotent end to end.
    """
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    stats_str = json.dumps(stats_json) if stats_json is not None else None
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO session_report
                   (report_id, session_id, dog_id, generated_at, model_id,
                    input_hash, summary_text, stats_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, input_hash) DO NOTHING""",
            (report_id, session_id, dog_id, generated_at, model_id,
             input_hash, summary_text, stats_str, now, now),
        )
        conn.commit()
    stored = get_session_report(session_id, input_hash)
    return stored if stored is not None else {
        "report_id": report_id, "session_id": session_id, "dog_id": dog_id,
        "generated_at": generated_at, "model_id": model_id, "input_hash": input_hash,
        "summary_text": summary_text, "stats_json": stats_json,
        "created_at": now, "updated_at": now,
    }


# ============== Sync Watermarks (Chain STORE ingest, spec section 9) ==============

def get_sync_state(table_name: str) -> Optional[dict]:
    """Return the incremental-sync watermark for one table, or None.

    Shape is verbatim from WIMZ_Data_Architecture_Spec.md section 4 — keyed by
    table_name only (do not add per-device keys without bumping the spec).
    """
    with db_connection() as conn:
        row = conn.execute(
            """SELECT table_name, last_synced_at, last_attempt_at, last_error
               FROM sync_state WHERE table_name = ?""",
            (table_name,),
        ).fetchone()
    return dict(row) if row else None


def advance_sync_state(
    table_name: str,
    last_synced_at: int,
    last_error: Optional[str] = None,
) -> None:
    """Advance the high-water mark for one table after an ingest batch.

    last_synced_at only moves forward (MAX) so out-of-order/retried batches never
    rewind the watermark. Records the attempt time and any error for observability.
    """
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO sync_state (table_name, last_synced_at, last_attempt_at, last_error)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(table_name) DO UPDATE SET
                   last_synced_at  = MAX(sync_state.last_synced_at, excluded.last_synced_at),
                   last_attempt_at = excluded.last_attempt_at,
                   last_error      = excluded.last_error""",
            (table_name, last_synced_at, now, last_error),
        )
        conn.commit()




# ============== Robot OTA Releases ==============
def _release_row_to_manifest(row: sqlite3.Row) -> dict:
    """Shape a releases row into the flat manifest the robot and app expect.

    Flat by contract: the app's RobotApi.getLatestRelease() checks
    ``data['version'] != null`` on the top-level object, so wrapping the manifest
    under any key makes the app read it as "no release available".
    """
    return {
        "version": row["version"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "size": row["size_bytes"],  # robot manifest key (updater contract)
        "url": f"/api/releases/{row['version']}/download",
        "notes": row["notes"] or "",
        "created_at": row["created_at"],
        "min_updater_version": row["min_updater_version"],
        # False once the artifact bytes have been pruned by retention. The
        # manifest row survives so an old version reads as "existed, no longer
        # downloadable" rather than "never existed".
        "artifact_available": row["pruned_at"] is None,
        "pruned_at": row["pruned_at"],
    }


def get_release(version: str) -> Optional[dict]:
    """Return the manifest for a version, or None if unknown."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM releases WHERE version = ?", (version,))
        row = cursor.fetchone()

    return _release_row_to_manifest(row) if row else None


def get_latest_release() -> Optional[dict]:
    """Return the most recently uploaded release manifest, or None if none exist.

    Ordered by upload time, not version string — versions carry no orderable
    semantics (the app compares them for inequality only).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM releases ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()

    return _release_row_to_manifest(row) if row else None


def get_release_filename(version: str) -> Optional[str]:
    """Return the stored artifact filename for a version, or None if unknown."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM releases WHERE version = ?", (version,))
        row = cursor.fetchone()

    return row["filename"] if row else None


def insert_release(
    version: str,
    sha256: str,
    size_bytes: int,
    filename: str,
    notes: Optional[str] = None,
    min_updater_version: Optional[str] = None,
) -> dict:
    """Record an uploaded release. Raises ValueError if the version already exists.

    Versions are never overwritten: the app treats any change in the version
    string as "update available" with no ordering, so silently replacing an
    artifact under a live version would leave already-updated robots believing
    they are current.
    """
    created_at = datetime.now(timezone.utc).isoformat()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR IGNORE INTO releases
               (version, sha256, size_bytes, filename, notes, min_updater_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (version, sha256, size_bytes, filename, notes, min_updater_version, created_at),
        )
        inserted = cursor.rowcount > 0
        conn.commit()

    if not inserted:
        raise ValueError(f"Release {version} already exists")

    logger.info(f"[RELEASE] Recorded {version} ({size_bytes} bytes, sha256={sha256[:12]}…)")
    return get_release(version)


def list_releases() -> list[dict]:
    """Return all release manifests, newest upload first."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM releases ORDER BY created_at DESC")
        rows = cursor.fetchall()

    return [_release_row_to_manifest(row) for row in rows]


def select_releases_to_prune(keep: int) -> list[dict]:
    """Return releases whose artifact bytes should be dropped: everything past
    the newest ``keep``, that still has bytes.

    ``keep`` is floored at 1 so the latest release can never be selected — a
    misconfigured 0 would otherwise prune the version the app is actively
    offering.
    """
    keep = max(1, keep)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT version, filename FROM releases
               WHERE pruned_at IS NULL
               ORDER BY created_at DESC
               LIMIT -1 OFFSET ?""",
            (keep,),
        )
        rows = cursor.fetchall()

    return [{"version": r["version"], "filename": r["filename"]} for r in rows]


def mark_release_pruned(version: str) -> None:
    """Record that a release's artifact bytes were removed. The row stays."""
    pruned_at = datetime.now(timezone.utc).isoformat()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE releases SET pruned_at = ? WHERE version = ? AND pruned_at IS NULL",
            (pruned_at, version),
        )
        conn.commit()


def is_release_pruned(version: str) -> Optional[bool]:
    """True/False for a known version, None if the version does not exist."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pruned_at FROM releases WHERE version = ?", (version,))
        row = cursor.fetchone()

    return None if row is None else row["pruned_at"] is not None


def delete_release(version: str) -> bool:
    """Remove a release row. Returns True if a row was deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM releases WHERE version = ?", (version,))
        deleted = cursor.rowcount > 0
        conn.commit()

    return deleted


# ============================================================================
# Dec 9 demo build (2026-11-06): event store, settings, notify, treat ledger
# ============================================================================

def get_dog_owner(dog_id: str) -> Optional[str]:
    """Owner user_id for a dog: user_dogs role=owner first, then dogs.user_id."""
    with db_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM user_dogs WHERE dog_id = ? AND role = 'owner' ORDER BY user_id LIMIT 1",
            (dog_id,),
        ).fetchone()
        if row:
            return row["user_id"]
        row = conn.execute("SELECT user_id FROM dogs WHERE id = ?", (dog_id,)).fetchone()
    return row["user_id"] if row else None


# ---- events ----

def insert_event(
    event_id: str,
    dog_id: Optional[str],
    device_id: str,
    user_id: Optional[str],
    type: str,
    ts: str,
    duration_s: Optional[float],
    data: dict,
    source: str,
    local_date: str,
) -> bool:
    """INSERT OR IGNORE one event row. Returns True if inserted, False if duplicate."""
    with db_connection() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO events
               (event_id, dog_id, device_id, user_id, type, ts, duration_s, data, source, local_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, dog_id, device_id, user_id, type, ts, duration_s, json.dumps(data), source,
             local_date, datetime.now(timezone.utc).isoformat()),
        )
        inserted = cur.rowcount > 0
        conn.commit()
    return inserted


def _row_to_event(row) -> dict:
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except (TypeError, ValueError):
        data = {}
    return {
        "event_id": row["event_id"],
        "dog_id": row["dog_id"],
        "device_id": row["device_id"],
        "user_id": row["user_id"],
        "type": row["type"],
        "ts": row["ts"],
        "duration_s": row["duration_s"],
        "data": data,
        "source": row["source"],
        "local_date": row["local_date"],
    }


def list_events(
    dog_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    types: Optional[list[str]] = None,
    limit: int = 100,
    order: str = "desc",
    local_date: Optional[str] = None,
) -> list[dict]:
    """Query events for a dog (ts is ISO-8601 UTC so string comparison is chronological)."""
    sql = "SELECT * FROM events WHERE dog_id = ?"
    params: list = [dog_id]
    if local_date:
        sql += " AND local_date = ?"
        params.append(local_date)
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if until:
        sql += " AND ts <= ?"
        params.append(until)
    if types:
        sql += f" AND type IN ({','.join('?' for _ in types)})"
        params.extend(types)
    sql += f" ORDER BY ts {'ASC' if order == 'asc' else 'DESC'} LIMIT ?"
    params.append(int(limit))
    with db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(r) for r in rows]


def get_latest_event(dog_id: str, type: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE dog_id = ? AND type = ? ORDER BY ts DESC LIMIT 1",
            (dog_id, type),
        ).fetchone()
    return _row_to_event(row) if row else None


# ---- device_dogs (pairing, 2026-11-07) ----

def get_device_dog_ids(device_id: str) -> list[str]:
    """Dogs paired with a robot via device_dogs, oldest link first. Only dogs the robot's
    CURRENT owner owns count (a robot handed to a new owner drops the old owner's dogs)."""
    with db_connection() as conn:
        rows = conn.execute(
            f"""SELECT d.id FROM device_dogs dd
                JOIN robots r ON r.device_id = dd.device_id AND r.owner_user_id IS NOT NULL
                JOIN dogs d ON d.id = dd.dog_id AND {_OWNED_BY_ROBOT_OWNER_SQL}
                WHERE dd.device_id = ?
                ORDER BY dd.created_at ASC, d.created_at ASC, d.id ASC""",
            (device_id,),
        ).fetchall()
    return [r["id"] for r in rows]


def get_dog_device_ids(dog_id: str) -> list[str]:
    """Robots a dog is paired with via device_dogs (owner-checked), oldest link first."""
    with db_connection() as conn:
        rows = conn.execute(
            f"""SELECT dd.device_id FROM device_dogs dd
                JOIN robots r ON r.device_id = dd.device_id AND r.owner_user_id IS NOT NULL
                JOIN dogs d ON d.id = dd.dog_id AND {_OWNED_BY_ROBOT_OWNER_SQL}
                WHERE dd.dog_id = ?
                ORDER BY dd.created_at ASC, dd.device_id ASC""",
            (dog_id,),
        ).fetchall()
    return [r["device_id"] for r in rows]


def get_owned_dog_ids(user_id: str) -> list[str]:
    """Dogs the user owns (user_dogs role=owner, else dogs.user_id), oldest first."""
    with db_connection() as conn:
        rows = conn.execute(
            """SELECT d.id FROM dogs d
               WHERE d.id IN (SELECT ud.dog_id FROM user_dogs ud WHERE ud.user_id = ? AND ud.role = 'owner')
                  OR (d.user_id = ? AND NOT EXISTS (SELECT 1 FROM user_dogs ux WHERE ux.dog_id = d.id AND ux.role = 'owner'))
               ORDER BY d.created_at ASC, d.id ASC""",
            (user_id, user_id),
        ).fetchall()
    return [r["id"] for r in rows]


def link_device_dog(device_id: str, dog_id: str) -> None:
    with db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO device_dogs (device_id, dog_id, created_at) VALUES (?, ?, ?)",
                     (device_id, dog_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()


def set_device_dogs(device_id: str, dog_ids: list[str]) -> None:
    """Replace a robot's pairing with exactly these dogs (order = link order). Events are
    keyed by dog_id, so unlinking a dog never touches its history."""
    now = datetime.now(timezone.utc)
    with db_connection() as conn:
        conn.execute("DELETE FROM device_dogs WHERE device_id = ?", (device_id,))
        for i, dog_id in enumerate(dict.fromkeys(dog_ids)):
            conn.execute("INSERT INTO device_dogs (device_id, dog_id, created_at) VALUES (?, ?, ?)",
                         (device_id, dog_id, (now + timedelta(microseconds=i)).isoformat()))
        conn.commit()


def create_placeholder_dog(dog_id: str, name: str, user_id: str) -> dict:
    """Owner-owned dog row flagged is_placeholder (the robot's default until re-paired)."""
    dog = create_dog(dog_id=dog_id, name=name, user_id=user_id)
    with db_connection() as conn:
        conn.execute("UPDATE dogs SET is_placeholder = 1 WHERE id = ?", (dog_id,))
        conn.commit()
    add_user_dog(user_id, dog_id, "owner")
    dog["is_placeholder"] = True
    return dog


def get_latest_status_for_devices(device_ids: list[str]) -> Optional[dict]:
    """Latest `status` event across the given robots, keyed by device_id (status is device-scoped)."""
    ids = [d for d in dict.fromkeys(device_ids) if d]
    if not ids:
        return None
    with db_connection() as conn:
        row = conn.execute(
            f"SELECT * FROM events WHERE type = 'status' AND device_id IN ({','.join('?' for _ in ids)}) "
            "ORDER BY ts DESC LIMIT 1",
            ids,
        ).fetchone()
    return _row_to_event(row) if row else None



def count_events(dog_id: str, type: str, local_date: Optional[str] = None, since_ts: Optional[str] = None) -> int:
    sql = "SELECT COALESCE(SUM(COALESCE(json_extract(data, '$.count'), 1)), 0) AS n FROM events WHERE dog_id = ? AND type = ?"
    params: list = [dog_id, type]
    if local_date:
        sql += " AND local_date = ?"
        params.append(local_date)
    if since_ts:
        sql += " AND ts >= ?"
        params.append(since_ts)
    with db_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"] or 0)


def delete_events_by_source(dog_id: str, source: str) -> list[str]:
    """Delete a dog's events of one source; returns the deleted event_ids."""
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT event_id FROM events WHERE dog_id = ? AND source = ?", (dog_id, source)
        ).fetchall()
        ids = [r["event_id"] for r in rows]
        conn.execute("DELETE FROM events WHERE dog_id = ? AND source = ?", (dog_id, source))
        conn.commit()
    return ids


def delete_activity_events_by_ids(event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    deleted = 0
    with db_connection() as conn:
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            cur = conn.execute(
                f"DELETE FROM activity_events WHERE id IN ({','.join('?' for _ in chunk)})", chunk
            )
            deleted += cur.rowcount
        conn.commit()
    return deleted


# ---- daily_summary cache ----

def get_daily_summary_row(dog_id: str, date_str: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE dog_id = ? AND date = ?", (dog_id, date_str)
        ).fetchone()
    if not row:
        return None
    return {
        "dog_id": row["dog_id"],
        "date": row["date"],
        "tz": row["tz"],
        "summary": json.loads(row["summary"]),
        "event_count": row["event_count"],
        "last_event_ts": row["last_event_ts"],
        "dirty": bool(row["dirty"]),
        "computed_at": row["computed_at"],
    }


def upsert_daily_summary(dog_id: str, date_str: str, tz: str, summary: dict,
                         event_count: int, last_event_ts: Optional[str]) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO daily_summary (dog_id, date, tz, summary, event_count, last_event_ts, dirty, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(dog_id, date) DO UPDATE SET
                 tz = excluded.tz, summary = excluded.summary, event_count = excluded.event_count,
                 last_event_ts = excluded.last_event_ts, dirty = 0, computed_at = excluded.computed_at""",
            (dog_id, date_str, tz, json.dumps(summary), event_count, last_event_ts,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def list_device_events(device_id: str, types: list[str], since: Optional[str] = None,
                       until: Optional[str] = None, limit: int = 20000) -> list[dict]:
    """A robot's events of the given types, oldest first, with since <= ts < until."""
    sql = f"SELECT * FROM events WHERE device_id = ? AND type IN ({','.join('?' for _ in types)})"
    params: list = [device_id, *types]
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if until:
        sql += " AND ts < ?"
        params.append(until)
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(int(limit))
    with db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(r) for r in rows]


def last_device_event_before(device_id: str, type: str, before: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM events WHERE device_id = ? AND type = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                           (device_id, type, before)).fetchone()
    return _row_to_event(row) if row else None


def mark_summaries_dirty_from(dog_ids: list[str], date_str: str) -> None:
    """Mark every cached summary on or after date_str dirty for these dogs."""
    if not dog_ids:
        return
    with db_connection() as conn:
        conn.execute(f"UPDATE daily_summary SET dirty = 1 WHERE date >= ? AND dog_id IN ({','.join('?' for _ in dog_ids)})",
                     (date_str, *dog_ids))
        conn.commit()


def mark_summary_dirty(dog_id: str, date_str: str) -> None:
    with db_connection() as conn:
        conn.execute(
            "UPDATE daily_summary SET dirty = 1 WHERE dog_id = ? AND date = ?", (dog_id, date_str)
        )
        conn.commit()


def delete_daily_summaries(dog_id: str) -> int:
    """Drop a dog's cached roll-ups (daily and weekly). Returns daily rows deleted."""
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM daily_summary WHERE dog_id = ?", (dog_id,))
        conn.execute("DELETE FROM weekly_summary WHERE dog_id = ?", (dog_id,))
        conn.commit()
        return cur.rowcount


# ---- weekly_summary cache ----

def get_weekly_summary_row(dog_id: str, week_start: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM weekly_summary WHERE dog_id = ? AND week_start = ?", (dog_id, week_start)
        ).fetchone()
    if not row:
        return None
    return {
        "dog_id": row["dog_id"],
        "week_start": row["week_start"],
        "tz": row["tz"],
        "summary": json.loads(row["summary"]),
        "event_count": row["event_count"],
        "last_event_ts": row["last_event_ts"],
        "dirty": bool(row["dirty"]),
        "computed_at": row["computed_at"],
    }


def upsert_weekly_summary(dog_id: str, week_start: str, tz: str, summary: dict,
                          event_count: int, last_event_ts: Optional[str]) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO weekly_summary (dog_id, week_start, tz, summary, event_count, last_event_ts, dirty, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(dog_id, week_start) DO UPDATE SET
                 tz = excluded.tz, summary = excluded.summary, event_count = excluded.event_count,
                 last_event_ts = excluded.last_event_ts, dirty = 0, computed_at = excluded.computed_at""",
            (dog_id, week_start, tz, json.dumps(summary), event_count, last_event_ts,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def daily_rows_fresh_for_week(dog_id: str, first_date: str, last_date: str, since: str) -> bool:
    """True when every day in [first_date, last_date] has a clean daily_summary row computed
    no later than `since` -- i.e. a weekly row computed at `since` still matches its days."""
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(dirty), 0) AS dirty, MAX(computed_at) AS latest "
            "FROM daily_summary WHERE dog_id = ? AND date >= ? AND date <= ?",
            (dog_id, first_date, last_date),
        ).fetchone()
    want = (datetime.strptime(last_date, "%Y-%m-%d") - datetime.strptime(first_date, "%Y-%m-%d")).days + 1
    return row["n"] == want and row["dirty"] == 0 and (row["latest"] or "") <= since


# ---- dog_settings ----

def get_dog_setting_row(dog_id: str, key: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM dog_settings WHERE dog_id = ? AND key = ?", (dog_id, key)
        ).fetchone()
    if not row:
        return None
    return {
        "key": row["key"],
        "value": json.loads(row["value"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def get_all_dog_setting_rows(dog_id: str) -> dict[str, dict]:
    with db_connection() as conn:
        rows = conn.execute("SELECT * FROM dog_settings WHERE dog_id = ?", (dog_id,)).fetchall()
    out = {}
    for row in rows:
        out[row["key"]] = {
            "key": row["key"],
            "value": json.loads(row["value"]),
            "version": row["version"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
    return out


def put_dog_setting(dog_id: str, key: str, value, updated_by: str,
                    expected_version: Optional[int] = None) -> Optional[dict]:
    """Upsert a setting. Returns the new row, or None on an expected_version mismatch."""
    now = datetime.now(timezone.utc).isoformat()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT version FROM dog_settings WHERE dog_id = ? AND key = ?", (dog_id, key)
        ).fetchone()
        current = row["version"] if row else 0
        if expected_version is not None and expected_version != current:
            return None
        new_version = current + 1
        conn.execute(
            """INSERT INTO dog_settings (dog_id, key, value, version, updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(dog_id, key) DO UPDATE SET
                 value = excluded.value, version = excluded.version,
                 updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
            (dog_id, key, json.dumps(value), new_version, now, updated_by),
        )
        conn.commit()
    return {"key": key, "value": value, "version": new_version, "updated_at": now, "updated_by": updated_by}


def delete_dog_setting(dog_id: str, key: str) -> bool:
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM dog_settings WHERE dog_id = ? AND key = ?", (dog_id, key))
        conn.commit()
        return cur.rowcount > 0


def insert_settings_audit(dog_id: str, key: str, actor: str, action: str,
                          old_value=None, new_value=None, reason: Optional[str] = None) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO settings_audit (dog_id, key, actor, action, old_value, new_value, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (dog_id, key, actor, action,
             json.dumps(old_value) if old_value is not None else None,
             json.dumps(new_value) if new_value is not None else None,
             reason, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def list_settings_audit(dog_id: str, limit: int = 50) -> list[dict]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM settings_audit WHERE dog_id = ? ORDER BY id DESC LIMIT ?", (dog_id, int(limit))
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "key": r["key"], "actor": r["actor"], "action": r["action"],
            "old_value": json.loads(r["old_value"]) if r["old_value"] else None,
            "new_value": json.loads(r["new_value"]) if r["new_value"] else None,
            "reason": r["reason"], "created_at": r["created_at"],
        })
    return out


# ---- notify_log ----

def insert_notify_log(dog_id: str, user_id: Optional[str], rule_id: str, event_id: str, event_type: str,
                      outcome: str, title: Optional[str] = None, body: Optional[str] = None,
                      image_url: Optional[str] = None, clip_url: Optional[str] = None) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO notify_log (dog_id, user_id, rule_id, event_id, event_type, outcome, title, body,
                                       image_url, clip_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dog_id, user_id, rule_id, event_id, event_type, outcome, title, body, image_url, clip_url,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def last_notify_sent(dog_id: str, rule_id: str) -> Optional[str]:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM notify_log WHERE dog_id = ? AND rule_id = ? AND outcome = 'sent' "
            "ORDER BY id DESC LIMIT 1",
            (dog_id, rule_id),
        ).fetchone()
    return row["created_at"] if row else None


def list_notify_log(dog_id: str, limit: int = 50) -> list[dict]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM notify_log WHERE dog_id = ? ORDER BY id DESC LIMIT ?", (dog_id, int(limit))
        ).fetchall()
    return [dict(r) for r in rows]


def delete_notify_log_for_dog(dog_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM notify_log WHERE dog_id = ?", (dog_id,))
        conn.commit()
        return cur.rowcount


def delete_old_notify_log(days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM notify_log WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


# ---- treat_ledger ----

def insert_treat_ledger(command_id: str, dog_id: str, actor: str, local_date: str) -> None:
    with db_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO treat_ledger (command_id, dog_id, actor, local_date, issued_at) VALUES (?, ?, ?, ?, ?)",
            (command_id, dog_id, actor, local_date, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def link_treat_ledger(command_id: str, event_id: str) -> bool:
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE treat_ledger SET event_id = ? WHERE command_id = ? AND event_id IS NULL",
            (event_id, command_id),
        )
        conn.commit()
        return cur.rowcount > 0


def count_pending_treat_ledger(dog_id: str, local_date: str, within_seconds: int = 120) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM treat_ledger WHERE dog_id = ? AND local_date = ? "
            "AND event_id IS NULL AND issued_at > ?",
            (dog_id, local_date, cutoff),
        ).fetchone()
    return int(row["n"] or 0)


def delete_treat_ledger_for_dog(dog_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM treat_ledger WHERE dog_id = ?", (dog_id,))
        conn.commit()
        return cur.rowcount


def delete_notify_log_by_event_ids(event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    deleted = 0
    with db_connection() as conn:
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            cur = conn.execute(
                f"DELETE FROM notify_log WHERE event_id IN ({','.join('?' for _ in chunk)})", chunk
            )
            deleted += cur.rowcount
        conn.commit()
    return deleted


def delete_events_by_ids(event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    deleted = 0
    with db_connection() as conn:
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            cur = conn.execute(
                f"DELETE FROM events WHERE event_id IN ({','.join('?' for _ in chunk)})", chunk
            )
            deleted += cur.rowcount
        conn.commit()
    return deleted


def get_event(event_id: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return _row_to_event(row) if row else None


# ---- mission_drafts ----

def _row_to_draft(row) -> dict:
    return {
        "draft_id": row["draft_id"], "dog_id": row["dog_id"], "user_id": row["user_id"], "actor": row["actor"],
        "draft": json.loads(row["draft"]), "warnings": json.loads(row["warnings"]) if row["warnings"] else [],
        "note": row["note"], "status": row["status"], "mission_id": row["mission_id"],
        "created_at": row["created_at"], "decided_at": row["decided_at"], "decided_by": row["decided_by"],
    }


def insert_mission_draft(draft_id: str, dog_id: str, user_id: Optional[str], actor: str, draft: dict,
                         warnings: list[str], note: str) -> dict:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO mission_drafts (draft_id, dog_id, user_id, actor, draft, warnings, note, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (draft_id, dog_id, user_id, actor, json.dumps(draft), json.dumps(warnings), note,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM mission_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row)


def get_mission_draft(draft_id: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM mission_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def list_mission_drafts(dog_id: str, status: Optional[str] = None, limit: int = 50) -> list[dict]:
    with db_connection() as conn:
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM mission_drafts WHERE dog_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
                (dog_id, status, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mission_drafts WHERE dog_id = ? ORDER BY created_at DESC LIMIT ?",
                (dog_id, int(limit))).fetchall()
    return [_row_to_draft(r) for r in rows]


def decide_mission_draft(draft_id: str, status: str, decided_by: str, mission_id: Optional[str] = None) -> bool:
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE mission_drafts SET status = ?, decided_at = ?, decided_by = ?, mission_id = COALESCE(?, mission_id) "
            "WHERE draft_id = ? AND status = 'pending'",
            (status, datetime.now(timezone.utc).isoformat(), decided_by, mission_id, draft_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_mission_drafts_for_dog(dog_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM mission_drafts WHERE dog_id = ?", (dog_id,))
        conn.commit()
        return cur.rowcount

# Initialize database on module import
init_db()
seed_default_pairings()
