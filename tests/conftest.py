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


# ---- ROBOT-STATE v2 (2026-11-19): a fake robot on robot_t, owned by user_t1 ----

ELSA = "019f58ad-03c9-7aff-adae-b697fb058ffc"
BEZIK = "01a183fe-0169-7d8b-ac62-5d58727473bf"


class FakeRobot:
    """Answers every command (C1 set_mode / C2 select_dog / C3 set_sleep with their contract results,
    any other command with success and an empty result) through the relay's real WS handler after
    `delay` s (`wake_delay` for set_sleep false), or never when `respond` is False. Commands named in
    `expire` get success false, error "expired" (the robot could not begin them in time). Drive frames
    get no answer. Posts mode_change (E1) on every mode change. Records everything the relay sends it
    and every frame the relay sends the owner's apps."""

    def __init__(self):
        import time as _t
        self._t = _t
        self.device_id = "robot_t"
        self.delay, self.respond, self.wake_delay = 0.05, True, None
        self.expire: set[str] = set()
        self.online = False
        self.mode, self.asleep, self.selected, self.session = "idle", False, None, None
        self.sent: list[dict] = []
        self.app_frames: list[tuple[float, dict]] = []
        self.hooks: list[tuple] = []
        self.mode_since = _t.monotonic()

    # transport
    def connect(self):
        from app.connection_manager import get_connection_manager
        from app.services import robot_state
        self.online = True
        get_connection_manager().robot_connections[self.device_id] = object()
        robot_state.on_robot_connected(self.device_id)

    def disconnect(self):
        from app.connection_manager import get_connection_manager
        from app.services import robot_state
        self.online = False
        get_connection_manager().robot_connections.pop(self.device_id, None)
        robot_state.on_robot_disconnected(self.device_id)

    async def send(self, device_id, message):
        if not self.online:
            return False
        self.sent.append(dict(message))
        if message.get("type") == "command" and message.get("command") not in ("motor", "servo", "pan_tilt") \
                and self.respond:
            import asyncio as _a
            _a.get_running_loop().create_task(self._answer(message))
        return True

    async def to_apps(self, user_id, message):
        self.app_frames.append((self._t.monotonic(), dict(message)))
        return 1

    def commands(self):
        return [m.get("command") for m in self.sent]

    def states(self):
        return [m for _, m in self.app_frames if m.get("type") == "robot_state"]

    # robot behaviour
    async def _answer(self, message):
        import asyncio as _a
        from app.routers import websocket as ws
        cmd, d = message["command"], message.get("data") or {}
        waking = cmd == "set_sleep" and d.get("asleep") is False
        await _a.sleep(self.wake_delay if waking and self.wake_delay is not None else self.delay)
        if cmd in self.expire:
            await ws._handle_robot_command_result(self.device_id, {
                "type": "command_result", "command_id": message["command_id"], "command": cmd, "success": False,
                "result": None, "error": "expired", "duration_ms": int(self.delay * 1000)})
            return
        if cmd not in ("set_mode", "set_sleep", "select_dog"):
            await ws._handle_robot_command_result(self.device_id, {
                "type": "command_result", "command_id": message["command_id"], "command": cmd, "success": True,
                "result": {}, "error": None, "duration_ms": int(self.delay * 1000)})
            return
        prev, prev_since = self.mode, self.mode_since
        if cmd == "select_dog":
            self.selected = d.get("dog_id")
            result = {"selected_dog_id": self.selected}
        elif cmd == "set_mode":
            self.asleep = False
            self.mode = d["mode"]
            self.session = (d.get("dog_id") or self.selected) if self.mode in ("silent_guardian", "coach", "mission") else None
            result = {"mode": self.mode, "session_dog_id": self.session}
        else:
            self.asleep = bool(d["asleep"])
            if self.asleep:
                self.mode, self.session = "idle", None
            result = {"asleep": self.asleep, "mode": self.mode}
        if self.mode != prev:
            self.mode_since = self._t.monotonic()
            await self.post_mode_change(prev, d.get("source"), d.get("reason"), message["command_id"],
                                        round(self.mode_since - prev_since, 1))
        await ws._handle_robot_command_result(self.device_id, {
            "type": "command_result", "command_id": message["command_id"], "command": cmd, "success": True,
            "result": result, "error": None, "duration_ms": int(self.delay * 1000)})

    async def post_mode_change(self, prev, source, reason, command_id, previous_duration_s=0.0):
        import uuid as _u
        from datetime import datetime as _dt, timezone as _tz
        from app.services import event_store
        await event_store.ingest_events([{
            "event_id": f"mc-{_u.uuid4()}", "type": "mode_change", "dog_id": None,
            "ts": _dt.now(_tz.utc).isoformat(),
            "data": {"mode": self.mode, "previous_mode": prev, "session_dog_id": self.session,
                     "source": source or "robot", "reason": reason, "command_id": command_id,
                     "previous_duration_s": previous_duration_s}}],
            device_id=self.device_id, user_id="user_t1", source="robot", notify=False, forward=False)


@pytest.fixture()
def robot(temp_db, monkeypatch):
    """robot_t paired to user_t1 (dogs Rex, Elsa, Bezik), offline until robot.connect()."""
    from app.connection_manager import get_connection_manager
    from app.services import notify_service, robot_state, sleep_wake
    db.create_device_pairing("user_t1", "robot_t")
    for dog_id, name in ((ELSA, "Elsa"), (BEZIK, "Bezik")):
        db.create_dog(dog_id=dog_id, name=name, user_id="user_t1")
        db.add_user_dog("user_t1", dog_id, "owner")
    sleep_wake.reset()
    robot_state._first_status_pending.clear()
    r = FakeRobot()
    manager = get_connection_manager()
    manager.device_owners["robot_t"] = "user_t1"
    manager.robot_connections.pop("robot_t", None)
    monkeypatch.setattr(manager, "send_to_robot", r.send)
    monkeypatch.setattr(manager, "send_to_user_apps", r.to_apps)
    monkeypatch.setattr(notify_service, "notify_agent",
                        lambda event, device_id, user_id, dog_id, payload=None, event_id=None:
                        r.hooks.append((event, device_id, user_id, payload)))
    yield r
    manager.robot_connections.pop("robot_t", None)
    manager.device_owners.pop("robot_t", None)
    sleep_wake.reset()
