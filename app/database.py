import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
            created_at TEXT NOT NULL
        )
    """)

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
        """INSERT INTO dogs (id, name, breed, color, profile_photo_url, aruco_marker_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (dog_id, name, breed, color, profile_photo_url, aruco_marker_id, created_at)
    )

    conn.commit()
    conn.close()

    return {
        "id": dog_id,
        "name": name,
        "breed": breed,
        "color": color,
        "profile_photo_url": profile_photo_url,
        "aruco_marker_id": aruco_marker_id,
        "created_at": created_at
    }


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


def delete_dog(dog_id: str) -> bool:
    """Delete a dog and its relationships."""
    conn = get_connection()
    cursor = conn.cursor()

    # Delete user_dogs relationships first
    cursor.execute("DELETE FROM user_dogs WHERE dog_id = ?", (dog_id,))
    # Delete dog photos
    cursor.execute("DELETE FROM dog_photos WHERE dog_id = ?", (dog_id,))
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

    conn.commit()
    conn.close()


# Initialize database on module import
init_db()
seed_default_pairings()
