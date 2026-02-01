import logging
import sqlite3
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path(__file__).parent.parent / "data" / "wimz.db"


def get_connection() -> sqlite3.Connection:
    """Get a database connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database with required tables."""
    conn = get_connection()
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

    # Index on dogs.user_id for fast lookup
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dogs_user_id ON dogs(user_id)")

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

    conn.commit()
    conn.close()


def create_user(user_id: str, email: str, hashed_password: str) -> dict:
    """Create a new user in the database."""
    conn = get_connection()
    cursor = conn.cursor()

    created_at = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        "INSERT INTO users (id, email, hashed_password, created_at) VALUES (?, ?, ?, ?)",
        (user_id, email, hashed_password, created_at)
    )

    conn.commit()
    conn.close()

    return {
        "user_id": user_id,
        "email": email,
        "created_at": created_at
    }


def get_user_by_email(email: str) -> Optional[dict]:
    """Get a user by email address."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()

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
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

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
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    conn.close()

    return count


def update_user_name(user_id: str, name: str) -> bool:
    """Update a user's name."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
    updated = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return updated


# ============== Dog CRUD Functions ==============

def get_dog_count() -> int:
    """Get the total number of dogs."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM dogs")
    count = cursor.fetchone()[0]
    conn.close()

    return count


def create_dog(
    dog_id: str,
    name: str,
    user_id: str,
    breed: Optional[str] = None,
    color: Optional[str] = None,
    profile_photo_url: Optional[str] = None,
    aruco_marker_id: Optional[int] = None
) -> dict:
    """Create a new dog in the database."""
    conn = get_connection()
    cursor = conn.cursor()

    created_at = datetime.now(timezone.utc).isoformat()

    cursor.execute(
        """INSERT INTO dogs (id, name, user_id, breed, color, profile_photo_url, aruco_marker_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (dog_id, name, user_id, breed, color, profile_photo_url, aruco_marker_id, created_at)
    )

    conn.commit()
    conn.close()

    return {
        "id": dog_id,
        "name": name,
        "user_id": user_id,
        "breed": breed,
        "color": color,
        "profile_photo_url": profile_photo_url,
        "aruco_marker_id": aruco_marker_id,
        "created_at": created_at
    }


def check_duplicate_dog_name(user_id: str, name: str) -> bool:
    """Check if a dog with the same name (case-insensitive) already exists for this user."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM dogs WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (user_id, name)
    )
    count = cursor.fetchone()[0]
    conn.close()

    return count > 0


def get_dog_by_id(dog_id: str) -> Optional[dict]:
    """Get a dog by ID."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "id": row["id"],
            "name": row["name"],
            "breed": row["breed"],
            "color": row["color"],
            "profile_photo_url": row["profile_photo_url"],
            "aruco_marker_id": row["aruco_marker_id"],
            "visual_features": row["visual_features"],
            "created_at": row["created_at"]
        }
    return None


def update_dog(dog_id: str, **fields) -> Optional[dict]:
    """Update a dog's fields."""
    if not fields:
        return get_dog_by_id(dog_id)

    conn = get_connection()
    cursor = conn.cursor()

    # Build dynamic UPDATE query
    set_clauses = ", ".join([f"{key} = ?" for key in fields.keys()])
    values = list(fields.values()) + [dog_id]

    cursor.execute(f"UPDATE dogs SET {set_clauses} WHERE id = ?", values)
    conn.commit()
    conn.close()

    return get_dog_by_id(dog_id)


def delete_dog(dog_id: str, user_id: str = None) -> bool:
    """Delete a dog and its relationships. If user_id is provided, only delete if owned by that user."""
    conn = get_connection()
    cursor = conn.cursor()

    # Verify ownership if user_id provided
    if user_id:
        cursor.execute("SELECT id FROM dogs WHERE id = ? AND user_id = ?", (dog_id, user_id))
        if not cursor.fetchone():
            conn.close()
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
    conn.close()

    return deleted


# ============== User-Dog Relationship Functions ==============

def add_user_dog(user_id: str, dog_id: str, role: str = "owner") -> dict:
    """Add a user-dog relationship."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR REPLACE INTO user_dogs (user_id, dog_id, role) VALUES (?, ?, ?)",
        (user_id, dog_id, role)
    )

    conn.commit()
    conn.close()

    return {"user_id": user_id, "dog_id": dog_id, "role": role}


def get_user_dogs(user_id: str) -> list[dict]:
    """Get all dogs for a user with their role."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT d.*, ud.role
        FROM dogs d
        JOIN user_dogs ud ON d.id = ud.dog_id
        WHERE ud.user_id = ?
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "breed": row["breed"],
            "color": row["color"],
            "profile_photo_url": row["profile_photo_url"],
            "aruco_marker_id": row["aruco_marker_id"],
            "visual_features": row["visual_features"],
            "created_at": row["created_at"],
            "role": row["role"]
        }
        for row in rows
    ]


def get_user_dog_role(user_id: str, dog_id: str) -> Optional[str]:
    """Get the user's role for a specific dog."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT role FROM user_dogs WHERE user_id = ? AND dog_id = ?",
        (user_id, dog_id)
    )
    row = cursor.fetchone()
    conn.close()

    return row["role"] if row else None


def remove_user_dog(user_id: str, dog_id: str) -> bool:
    """Remove a user-dog relationship."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM user_dogs WHERE user_id = ? AND dog_id = ?",
        (user_id, dog_id)
    )
    deleted = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return deleted


# ============== Dog Photo Functions ==============

def get_photo_count() -> int:
    """Get the total number of dog photos."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM dog_photos")
    count = cursor.fetchone()[0]
    conn.close()

    return count


def create_dog_photo(
    photo_id: str,
    dog_id: str,
    photo_url: str,
    is_profile_photo: bool = False
) -> dict:
    """Create a new dog photo."""
    conn = get_connection()
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
    conn.close()

    return {
        "id": photo_id,
        "dog_id": dog_id,
        "photo_url": photo_url,
        "is_profile_photo": is_profile_photo,
        "captured_at": captured_at
    }


def get_dog_photos(dog_id: str) -> list[dict]:
    """Get all photos for a dog."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM dog_photos WHERE dog_id = ? ORDER BY captured_at DESC",
        (dog_id,)
    )
    rows = cursor.fetchall()
    conn.close()

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
    conn = get_connection()
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
    conn.close()

    logger.info(f"[PAIRING] Created: device {device_id} -> user {user_id}")
    return {"user_id": user_id, "device_id": device_id, "paired_at": paired_at}


def delete_device_pairing(device_id: str) -> bool:
    """Remove device pairing (set owner to NULL)."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE robots SET owner_user_id = NULL WHERE device_id = ?",
        (device_id,)
    )
    deleted = cursor.rowcount > 0

    conn.commit()
    conn.close()

    if deleted:
        logger.info(f"[PAIRING] Deleted: device {device_id} unpaired")
    return deleted


def get_device_owner(device_id: str) -> Optional[str]:
    """Get the owner user_id for a device."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT owner_user_id FROM robots WHERE device_id = ?",
        (device_id,)
    )
    row = cursor.fetchone()
    conn.close()

    return row["owner_user_id"] if row else None


def get_all_device_pairings() -> dict[str, str]:
    """Get all device_id -> user_id pairings for loading into memory."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT device_id, owner_user_id FROM robots WHERE owner_user_id IS NOT NULL"
    )
    rows = cursor.fetchall()
    conn.close()

    return {row["device_id"]: row["owner_user_id"] for row in rows}


def get_user_paired_devices(user_id: str) -> list[str]:
    """Get all device_ids paired with a user."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT device_id FROM robots WHERE owner_user_id = ?",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    return [row["device_id"] for row in rows]


def seed_default_pairings():
    """Seed default pairings for testing if they don't exist."""
    conn = get_connection()
    cursor = conn.cursor()

    # Check if we already have pairings
    cursor.execute("SELECT COUNT(*) FROM robots WHERE owner_user_id IS NOT NULL")
    if cursor.fetchone()[0] > 0:
        conn.close()
        logger.debug("[PAIRING] Seed skipped - pairings already exist")
        return  # Already seeded

    now = datetime.now(timezone.utc).isoformat()

    # Seed default test pairings
    default_pairings = [
        ("robot_001", "wimz_robot_01", "user_000001", "WIM-Z Robot 01"),
        ("robot_002", "wimz_robot_02", "user_000001", "WIM-Z Robot 02"),
    ]

    for robot_id, device_id, owner_id, name in default_pairings:
        cursor.execute(
            """INSERT OR IGNORE INTO robots (id, device_id, owner_user_id, name, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (robot_id, device_id, owner_id, name, now)
        )
        logger.info(f"[PAIRING] Seeded: device {device_id} -> user {owner_id}")

    conn.commit()
    conn.close()


# ============== Dog Metrics Functions ==============

def log_metric(dog_id: str, user_id: str, metric_type: str, value: int = 1) -> dict:
    """Upsert a daily metric row. metric_type must be one of: treat_count, detection_count, session_minutes."""
    conn = get_connection()
    cursor = conn.cursor()

    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    # Validate metric_type is a known column
    valid_metrics = ("treat_count", "detection_count", "mission_attempts", "mission_successes", "session_minutes")
    if metric_type not in valid_metrics:
        conn.close()
        raise ValueError(f"Invalid metric_type: {metric_type}. Must be one of {valid_metrics}")

    cursor.execute(
        f"""INSERT INTO dog_metrics (dog_id, user_id, date, {metric_type}, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dog_id, date) DO UPDATE SET
                {metric_type} = {metric_type} + ?,
                updated_at = ?""",
        (dog_id, user_id, today, value, now, now, value, now)
    )

    conn.commit()
    conn.close()

    logger.info(f"[METRICS] Logged {metric_type}+={value} for dog {dog_id}")
    return {"dog_id": dog_id, "metric_type": metric_type, "value": value, "date": today}


def log_mission(dog_id: str, user_id: str, mission_type: str, result: str, details: str = None) -> dict:
    """Log a mission event and update daily metrics."""
    conn = get_connection()
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
    conn.close()

    logger.info(f"[METRICS] Mission logged: dog={dog_id}, type={mission_type}, result={result}")
    return {"dog_id": dog_id, "mission_type": mission_type, "result": result, "timestamp": now}


def get_metrics(dog_id: str, user_id: str, since_date: str = None) -> dict:
    """Get aggregated metrics for a dog since a given date. Returns summed totals."""
    conn = get_connection()
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
    conn.close()

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
    conn = get_connection()
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
    conn.close()

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
    conn = get_connection()
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
    conn.close()

    logger.info(f"[SCHEDULE] Created: {schedule_id} for user {user_id}, mission {mission_id}")
    return get_schedule_by_id(schedule_id)


def get_schedule_by_id(schedule_id: str) -> Optional[dict]:
    """Get a schedule by ID."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM mission_schedules WHERE id = ?", (schedule_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return _row_to_schedule(row)
    return None


def get_user_schedules(user_id: str) -> list[dict]:
    """Get all schedules for a user."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM mission_schedules WHERE user_id = ? ORDER BY hour, minute",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    return [_row_to_schedule(row) for row in rows]


def update_schedule(schedule_id: str, user_id: str, **fields) -> Optional[dict]:
    """Update a schedule. Returns None if not found or not owned by user."""
    conn = get_connection()
    cursor = conn.cursor()

    # Verify ownership
    cursor.execute("SELECT id FROM mission_schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id))
    if not cursor.fetchone():
        conn.close()
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
        conn.close()
        return get_schedule_by_id(schedule_id)

    now = datetime.now(timezone.utc).isoformat()
    fields["updated_at"] = now

    set_clauses = ", ".join([f"{key} = ?" for key in fields.keys()])
    values = list(fields.values()) + [schedule_id]

    cursor.execute(f"UPDATE mission_schedules SET {set_clauses} WHERE id = ?", values)
    conn.commit()
    conn.close()

    logger.info(f"[SCHEDULE] Updated: {schedule_id}")
    return get_schedule_by_id(schedule_id)


def delete_schedule(schedule_id: str, user_id: str) -> bool:
    """Delete a schedule. Returns False if not found or not owned by user."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM mission_schedules WHERE id = ? AND user_id = ?",
        (schedule_id, user_id)
    )
    deleted = cursor.rowcount > 0

    conn.commit()
    conn.close()

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
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT scheduling_enabled FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    # Default to enabled if no setting exists
    return bool(row["scheduling_enabled"]) if row else True


def set_scheduling_enabled(user_id: str, enabled: bool) -> bool:
    """Enable or disable scheduling for a user."""
    conn = get_connection()
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
    conn.close()

    logger.info(f"[SCHEDULE] User {user_id} scheduling {'enabled' if enabled else 'disabled'}")
    return True


# Initialize database on module import
init_db()
seed_default_pairings()
