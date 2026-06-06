import contextlib
import json
import logging
import sqlite3
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path(__file__).parent.parent / "data" / "wimz.db"


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

        # Replay buffer seq counters (persisted across restarts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS replay_seq (
                device_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL DEFAULT 0
            )
        """)

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
    "mode_changed", "dog_detected", "treat_dispensed", "bark_detected",
    "upload_complete", "upload_error", "upload_result",
    "audio_state",
    "schedule_created", "schedule_updated", "schedule_deleted", "schedule_triggered",
    "error",
}


def store_event(device_id: str, owner_user_id: str, event_type: str, message: dict) -> Optional[int]:
    """Store a robot event for offline retrieval. Returns row id or None if not storable."""
    if event_type not in STORABLE_EVENT_TYPES:
        return None

    with db_connection() as conn:
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        timestamp = message.get("timestamp", now)
        message_json = json.dumps(message)

        cursor.execute(
            """INSERT INTO device_events (device_id, owner_user_id, event_type, message, timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (device_id, owner_user_id, event_type, message_json, timestamp, now)
        )

        row_id = cursor.lastrowid
        conn.commit()

    logger.debug(f"[EVENT-STORE] Stored {event_type} for device {device_id} (id={row_id})")
    return row_id


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
    }


def query_activity_events(
    user_id: str,
    dog_id: Optional[str] = None,
    since: Optional[str] = None,
    cursor_ts: Optional[str] = None,
    cursor_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Query events, sorted by (timestamp DESC, id DESC). Returns up to `limit` rows.

    `cursor_ts` + `cursor_id` form the keyset cursor: rows strictly older than the cursor.
    """
    with db_connection() as conn:
        cursor = conn.cursor()

        conditions = ["user_id = ?"]
        params: list = [user_id]

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


# Initialize database on module import
init_db()
seed_default_pairings()
