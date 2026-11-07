"""Pytest fixtures: point the app's SQLite at a temp file, seed a user + dog."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = (ROOT / "data" / "wimz.db").resolve()
sys.path.insert(0, str(ROOT))
# Must be set BEFORE app.database is imported: its import runs init_db() (incl. migrations).
# Tests never open the real data/wimz.db.
_IMPORT_DB_DIR = tempfile.mkdtemp(prefix="wimz-test-")
os.environ["WIMZ_DB_PATH"] = os.path.join(_IMPORT_DB_DIR, "import.db")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")
os.environ.setdefault("ADMIN_EMAILS", "admin@example.com")
os.environ.setdefault("MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8001")

import app.database as db  # noqa: E402  (import runs init_db against the temp import.db above)

assert Path(db.DB_PATH).resolve() != REAL_DB, "tests must never open data/wimz.db"


@pytest.fixture(autouse=True)
def _never_real_db():
    yield
    assert Path(db.DB_PATH).resolve() != REAL_DB, "a test pointed DB_PATH at data/wimz.db"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    from app.mcp_server import oauth_db
    oauth_db.init_oauth_tables()
    db.create_user("user_t1", "owner@example.com", "x")
    db.create_dog(dog_id="dog_t1", name="Rex", user_id="user_t1")
    db.add_user_dog("user_t1", "dog_t1", "owner")
    db.create_user("user_t2", "viewer@example.com", "x")
    db.add_user_dog("user_t2", "dog_t1", "viewer")
    return path
