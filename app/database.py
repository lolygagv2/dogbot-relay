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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            created_at TEXT NOT NULL
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


# Initialize database on module import
init_db()
