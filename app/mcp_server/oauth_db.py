"""SQLite storage for the WIM-Z OAuth 2.1 authorization server + MCP audit log.

Same database file and connection helpers as the relay (`app.database`), WAL mode.
Tokens are stored hashed (sha256); the plaintext is only ever returned to the client
once. Client secrets are stored as issued because the SDK's client authenticator
compares the plaintext at /token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.database import db_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def init_oauth_tables() -> None:
    with db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id                  TEXT PRIMARY KEY,
                client_secret              TEXT,
                client_name                TEXT,
                token_endpoint_auth_method TEXT NOT NULL,
                redirect_uris              TEXT NOT NULL,
                metadata_json              TEXT NOT NULL,
                client_id_issued_at        INTEGER NOT NULL,
                created_ip                 TEXT,
                created_at                 TEXT NOT NULL,
                last_used_at               TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS oauth_login_sessions (
                sid          TEXT PRIMARY KEY,
                client_id    TEXT NOT NULL,
                params_json  TEXT NOT NULL,
                user_id      TEXT,
                csrf         TEXT NOT NULL,
                expires_at   REAL NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS oauth_codes (
                code                            TEXT PRIMARY KEY,
                client_id                       TEXT NOT NULL,
                user_id                         TEXT NOT NULL,
                scopes                          TEXT NOT NULL,
                code_challenge                  TEXT NOT NULL,
                redirect_uri                    TEXT NOT NULL,
                redirect_uri_provided_explicitly INTEGER NOT NULL,
                resource                        TEXT,
                family_id                       TEXT,
                expires_at                      REAL NOT NULL,
                used_at                         REAL,
                created_at                      TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                token_hash  TEXT PRIMARY KEY,
                token_type  TEXT NOT NULL CHECK (token_type IN ('access', 'refresh')),
                client_id   TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                scopes      TEXT NOT NULL,
                resource    TEXT,
                family_id   TEXT NOT NULL,
                expires_at  REAL NOT NULL,
                revoked_at  REAL,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_oauth_tokens_family ON oauth_tokens(family_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_oauth_tokens_user ON oauth_tokens(user_id, client_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS mcp_audit (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT NOT NULL,
                actor          TEXT NOT NULL,
                client_id      TEXT,
                client_name    TEXT,
                user_id        TEXT,
                dog_id         TEXT,
                tool           TEXT NOT NULL,
                scopes         TEXT,
                input_json     TEXT,
                outcome        TEXT NOT NULL,
                relay_status   INTEGER,
                result_summary TEXT,
                duration_ms    INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_audit_ts ON mcp_audit(ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mcp_audit_client ON mcp_audit(client_id, ts DESC)")
        conn.commit()


# ---- clients ----

def save_client(info: dict, created_ip: Optional[str] = None) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO oauth_clients
               (client_id, client_secret, client_name, token_endpoint_auth_method, redirect_uris, metadata_json,
                client_id_issued_at, created_ip, created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (info["client_id"], info.get("client_secret"), info.get("client_name"),
             info.get("token_endpoint_auth_method") or "none",
             json.dumps([str(u) for u in (info.get("redirect_uris") or [])]),
             json.dumps(info, default=str), int(info.get("client_id_issued_at") or time.time()),
             created_ip, _now()),
        )
        conn.commit()


def load_client(client_id: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    if not row:
        return None
    meta = json.loads(row["metadata_json"])
    meta["client_secret"] = row["client_secret"]
    meta["_client_name"] = row["client_name"]
    return meta


def touch_client(client_id: str) -> None:
    with db_connection() as conn:
        conn.execute("UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?", (_now(), client_id))
        conn.commit()


def registrations_since(ip: Optional[str], seconds: int) -> int:
    if not ip:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM oauth_clients WHERE created_ip = ? AND created_at > ?", (ip, cutoff)
        ).fetchone()
    return int(row["n"] or 0)


def list_clients() -> list[dict]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT client_id, client_name, token_endpoint_auth_method, created_at, last_used_at FROM oauth_clients "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---- login sessions ----

def create_login_session(sid: str, client_id: str, params: dict, csrf: str, ttl_s: int = 600) -> None:
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO oauth_login_sessions (sid, client_id, params_json, user_id, csrf, expires_at, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (sid, client_id, json.dumps(params, default=str), csrf, time.time() + ttl_s, _now()),
        )
        conn.commit()


def get_login_session(sid: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM oauth_login_sessions WHERE sid = ?", (sid,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return {"sid": row["sid"], "client_id": row["client_id"], "params": json.loads(row["params_json"]),
            "user_id": row["user_id"], "csrf": row["csrf"], "expires_at": row["expires_at"]}


def set_login_session_user(sid: str, user_id: str) -> None:
    with db_connection() as conn:
        conn.execute("UPDATE oauth_login_sessions SET user_id = ? WHERE sid = ?", (user_id, sid))
        conn.commit()


def delete_login_session(sid: str) -> None:
    with db_connection() as conn:
        conn.execute("DELETE FROM oauth_login_sessions WHERE sid = ?", (sid,))
        conn.commit()


# ---- authorization codes ----

def save_code(code: str, client_id: str, user_id: str, scopes: list[str], code_challenge: str,
              redirect_uri: str, redirect_uri_provided_explicitly: bool, resource: Optional[str],
              ttl_s: int = 300) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO oauth_codes (code, client_id, user_id, scopes, code_challenge, redirect_uri,
                                        redirect_uri_provided_explicitly, resource, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, client_id, user_id, " ".join(scopes), code_challenge, redirect_uri,
             1 if redirect_uri_provided_explicitly else 0, resource, time.time() + ttl_s, _now()),
        )
        conn.commit()


def load_code(code: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
    if not row:
        return None
    return {
        "code": row["code"], "client_id": row["client_id"], "user_id": row["user_id"],
        "scopes": row["scopes"].split(), "code_challenge": row["code_challenge"],
        "redirect_uri": row["redirect_uri"],
        "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
        "resource": row["resource"], "family_id": row["family_id"],
        "expires_at": row["expires_at"], "used_at": row["used_at"],
    }


def consume_code(code: str, family_id: str) -> bool:
    """Atomically mark a code used; False if it was already used (replay)."""
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE oauth_codes SET used_at = ?, family_id = ? WHERE code = ? AND used_at IS NULL",
            (time.time(), family_id, code),
        )
        conn.commit()
        return cur.rowcount > 0


# ---- tokens ----

def save_token(token: str, token_type: str, client_id: str, user_id: str, scopes: list[str],
               resource: Optional[str], family_id: str, expires_at: float) -> None:
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO oauth_tokens (token_hash, token_type, client_id, user_id, scopes, resource, family_id,
                                         expires_at, revoked_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (token_hash(token), token_type, client_id, user_id, " ".join(scopes), resource, family_id,
             expires_at, _now()),
        )
        conn.commit()


def load_token(token: str) -> Optional[dict]:
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ?", (token_hash(token),)).fetchone()
    if not row:
        return None
    return {
        "token_hash": row["token_hash"], "token_type": row["token_type"], "client_id": row["client_id"],
        "user_id": row["user_id"], "scopes": row["scopes"].split(), "resource": row["resource"],
        "family_id": row["family_id"], "expires_at": row["expires_at"], "revoked_at": row["revoked_at"],
    }


def revoke_token_hash(th: str) -> None:
    with db_connection() as conn:
        conn.execute("UPDATE oauth_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                     (time.time(), th))
        conn.commit()


def revoke_family(family_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute("UPDATE oauth_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                           (time.time(), family_id))
        conn.commit()
        return cur.rowcount


def revoke_access_tokens_in_family(family_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute(
            "UPDATE oauth_tokens SET revoked_at = ? WHERE family_id = ? AND token_type = 'access' AND revoked_at IS NULL",
            (time.time(), family_id))
        conn.commit()
        return cur.rowcount


def revoke_all_for_client(client_id: str) -> int:
    with db_connection() as conn:
        cur = conn.execute("UPDATE oauth_tokens SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                           (time.time(), client_id))
        conn.commit()
        return cur.rowcount


# ---- audit ----

def insert_audit(actor: str, tool: str, outcome: str, *, client_id: Optional[str] = None,
                 client_name: Optional[str] = None, user_id: Optional[str] = None, dog_id: Optional[str] = None,
                 scopes: Optional[list[str]] = None, input_obj: Any = None, relay_status: Optional[int] = None,
                 result_summary: Optional[str] = None, duration_ms: Optional[int] = None) -> None:
    try:
        input_json = json.dumps(input_obj, default=str)[:4096] if input_obj is not None else None
    except (TypeError, ValueError):
        input_json = None
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO mcp_audit (ts, actor, client_id, client_name, user_id, dog_id, tool, scopes, input_json,
                                      outcome, relay_status, result_summary, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), actor, client_id, client_name, user_id, dog_id, tool,
             " ".join(scopes) if scopes else None, input_json, outcome, relay_status,
             (result_summary or "")[:512] or None, duration_ms),
        )
        conn.commit()


def list_audit(limit: int = 50, client_id: Optional[str] = None) -> list[dict]:
    with db_connection() as conn:
        if client_id:
            rows = conn.execute("SELECT * FROM mcp_audit WHERE client_id = ? ORDER BY id DESC LIMIT ?",
                                (client_id, int(limit))).fetchall()
        else:
            rows = conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ---- retention ----

def sweep(audit_days: int = 180) -> dict:
    now = time.time()
    cutoff_audit = (datetime.now(timezone.utc) - timedelta(days=audit_days)).isoformat()
    with db_connection() as conn:
        a = conn.execute("DELETE FROM oauth_codes WHERE expires_at < ? OR used_at IS NOT NULL AND used_at < ?",
                         (now - 3600, now - 86400)).rowcount
        b = conn.execute("DELETE FROM oauth_login_sessions WHERE expires_at < ?", (now,)).rowcount
        c = conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now - 7 * 86400,)).rowcount
        d = conn.execute("DELETE FROM mcp_audit WHERE ts < ?", (cutoff_audit,)).rowcount
        e = conn.execute(
            """DELETE FROM oauth_clients WHERE created_at < ? AND client_id NOT IN
               (SELECT DISTINCT client_id FROM oauth_tokens)""",
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
        ).rowcount
        conn.commit()
    return {"codes": a, "sessions": b, "tokens": c, "audit": d, "clients": e}
