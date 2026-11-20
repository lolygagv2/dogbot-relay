"""ROBOT-STATE v2 (2026-11-19): the relay-owned state record, one per robot (device_id).

Fields (the E2 object):
  robot_online, mode (idle | silent_guardian | coach | mission | manual), asleep,
  selected_dog_id, selected_dog_applied, session_dog_id, state_version, updated_at, source, reason.
Sleep is not a mode: asleep=true forces mode "idle" and session_dog_id null.

Persisted in the `robot_state` table (data/wimz.db), so it survives relay restarts. Every read goes
to the table, so the voice-agent process can read it too; only the relay process writes it.

What changes the state:
  - robot connect / disconnect            -> robot_online (source "robot")
  - the robot's command_result for set_mode / set_sleep / select_dog (success true only)
  - the robot's mode_change events (POST /events), whatever caused them
  - the first status after a (re)connect -> mode (a robot may have rebooted); any status with a
    boolean `asleep` -> asleep
  - select_dog (H2): selected_dog_id at once, selected_dog_applied after the robot confirms it
Nothing is queued (R7): the one exception is select_dog, whose single stored value is sent once on
the robot's next connect while it is not applied (R8).

Every change bumps state_version and is pushed to the owner's app clients as a `robot_state` frame.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.database import db_connection, get_device_owner as db_get_device_owner

logger = logging.getLogger(__name__)

MODES = ("idle", "silent_guardian", "coach", "mission", "manual")
SESSION_MODES = {"silent_guardian", "coach", "mission"}
SOURCES = {"app", "connector", "agent", "schedule", "robot", "admin", "relay"}
STATE_COMMANDS = {"set_mode", "set_sleep", "select_dog"}
COMMAND_TIMEOUT_S = 2.0   # the robot answers C1 / C2 / C3 within 2000 ms

ERR_OFFLINE = "robot_offline"
ERR_TIMEOUT = "robot_timeout"
ERR_INVALID_MODE = "invalid_mode"

# Fields whose change bumps state_version.
_VERSIONED = ("robot_online", "mode", "asleep", "selected_dog_id", "selected_dog_applied", "session_dog_id")
_E2_KEYS = ("device_id", "robot_online", "mode", "asleep", "selected_dog_id", "selected_dog_applied",
            "session_dog_id", "state_version", "updated_at", "source", "reason")

# Robots whose first status after a (re)connect has not arrived yet (it resets mode).
_first_status_pending: set[str] = set()


def now_iso() -> str:
    """ISO-8601 UTC with milliseconds, e.g. 2026-11-19T18:47:32.551Z."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def source_for(actor_kind: Optional[str]) -> str:
    """The relay sets data.source from who called; callers never choose it."""
    return actor_kind if actor_kind in SOURCES and actor_kind != "robot" else "relay"


def valid_mode(raw: Any) -> Optional[str]:
    """One of the five modes, exactly as spelled, else None (-> invalid_mode)."""
    return raw if isinstance(raw, str) and raw in MODES else None


def normalize_mode(raw: Any) -> str:
    """Anything the robot reports that is not one of the five (night_sentry, photography, emergency,
    shutdown, docking, the retired "sleep", ...) reads as "idle" (R6)."""
    if isinstance(raw, str) and raw.strip().lower() in MODES:
        return raw.strip().lower()
    return "idle"


# ---------------------------------------------------------------- storage


def _default(device_id: str) -> dict:
    return {"device_id": device_id, "robot_online": False, "mode": "idle", "asleep": False,
            "selected_dog_id": None, "selected_dog_applied": False, "session_dog_id": None,
            "state_version": 0, "updated_at": now_iso(), "source": "relay", "reason": None,
            "select_command_id": None, "select_source": None}


def get(device_id: str) -> dict:
    """The full stored record (E2 fields plus internal select_* fields)."""
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM robot_state WHERE device_id = ?", (device_id,)).fetchone()
    if not row:
        return _default(device_id)
    st = dict(row)
    for k in ("robot_online", "asleep", "selected_dog_applied"):
        st[k] = bool(st[k])
    return st


def public(state_or_device: Any) -> dict:
    """The E2 / H1 object."""
    st = get(state_or_device) if isinstance(state_or_device, str) else state_or_device
    return {"type": "robot_state", **{k: st.get(k) for k in _E2_KEYS}}


def _save(st: dict) -> None:
    cols = ("device_id", "robot_online", "mode", "asleep", "selected_dog_id", "selected_dog_applied",
            "session_dog_id", "state_version", "updated_at", "source", "reason", "select_command_id",
            "select_source")
    vals = [int(st[c]) if isinstance(st.get(c), bool) else st.get(c) for c in cols]
    with db_connection() as conn:
        conn.execute(f"INSERT OR REPLACE INTO robot_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     vals)
        conn.commit()


def update(device_id: str, *, source: str, reason: Optional[str] = None, force: bool = False,
           **changes: Any) -> tuple[dict, bool]:
    """Apply changes with the R4/R5 rules; bump state_version and push when a versioned field changed
    (or force=True). Returns (state, changed)."""
    cur = get(device_id)
    new = {**cur, **changes}
    if new["asleep"]:                       # R5
        new["mode"], new["session_dog_id"] = "idle", None
    if new["mode"] not in SESSION_MODES:    # R4
        new["session_dog_id"] = None
    changed = force or any(new[k] != cur[k] for k in _VERSIONED)
    if changed:
        new["state_version"] = int(cur["state_version"]) + 1
        new["updated_at"] = now_iso()
        new["source"] = source if source in SOURCES else "relay"
        new["reason"] = str(reason)[:120] if reason is not None else None
    if changed or any(new.get(k) != cur.get(k) for k in ("select_command_id", "select_source")):
        _save(new)
    if changed:
        logger.info(f"[STATE] {device_id} v{new['state_version']} ({new['source']}/{new['reason']}): "
                    + ", ".join(f"{k}={new[k]}" for k in _VERSIONED if new[k] != cur[k]))
        _after_change(cur, new)
    return new, changed


def _owner(device_id: str) -> Optional[str]:
    try:
        from app.connection_manager import get_connection_manager
        owner = get_connection_manager().get_device_owner(device_id)
        if owner:
            return owner
    except Exception:  # pragma: no cover
        pass
    return db_get_device_owner(device_id)


def _spawn(coro) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()   # no loop (sync test code): nothing to push


_woke_at: dict[str, float] = {}   # device -> monotonic time it last went asleep -> awake (in-process)


def woke_within(device_id: Optional[str], seconds: float) -> bool:
    import time
    t = _woke_at.get(device_id or "")
    return t is not None and time.monotonic() - t < seconds


def _after_change(cur: dict, new: dict) -> None:
    import time
    device_id = new["device_id"]
    _spawn(push(device_id, new))
    if cur["asleep"] and not new["asleep"]:
        _woke_at[device_id] = time.monotonic()
    # The wake greeting is NOT fired here: only an explicit wake's command_result fires it (see
    # on_command_result). Going to sleep, or a new selection while asleep, has the voice agent
    # record the wake greeting ahead of time so it can go out within 1000 ms of the wake.
    if new["asleep"] and (not cur["asleep"] or new["selected_dog_id"] != cur["selected_dog_id"]):
        _agent_hook("greeting_prepare", device_id, {"cause": "asleep" if not cur["asleep"] else "selection"})


async def push(device_id: str, st: Optional[dict] = None) -> int:
    """E2: the state to every connected app client of the account."""
    owner = _owner(device_id)
    if not owner:
        return 0
    from app.connection_manager import get_connection_manager
    return await get_connection_manager().send_to_user_apps(owner, public(st or device_id))


async def send_all_to_app(send: Callable, device_ids: list[str]) -> None:
    """E2 on client connect: one robot_state frame per paired robot."""
    for d in device_ids:
        try:
            await send(public(d))
        except Exception as e:  # pragma: no cover - a closing socket
            logger.debug(f"[STATE] connect push to app failed: {e}")
            return


EXPLICIT_WAKE_SOURCES = {"app", "connector"}


def _agent_hook(event: str, device_id: str, payload: dict) -> None:
    try:
        from app.services.notify_service import notify_agent
        owner = _owner(device_id)
        if owner:
            notify_agent(event, device_id, owner, None, payload, f"{event}:{device_id}:{uuid.uuid4().hex[:8]}")
    except Exception as e:  # never let a greeting break a state change
        logger.debug(f"[AGENT-HOOK] {event} hook failed: {e}")


def mark_explicit_wake(command_id: str, device_id: str, source: Optional[str]) -> None:
    """An explicit wake (set_sleep {asleep: false} from the app or a connector, NOT the relay's
    wake-first): its successful command_result fires the wake greeting. The agent is told now so it
    can refresh the prepared greeting while the robot wakes."""
    from app.services import command_results
    meta = command_results._meta.get(command_id)
    if meta is None or source not in EXPLICIT_WAKE_SOURCES:
        return
    meta["explicit_wake"] = True
    _agent_hook("greeting_prepare", device_id, {"cause": "wake_requested"})


def _greet_on_wake(device_id: str, cause: Optional[str]) -> None:
    """Wake greeting (section 9): the voice agent names the selected dog read from this table."""
    import time
    logger.info(f"[STATE] {device_id}: explicit wake confirmed; wake greeting requested")
    _agent_hook("robot_woke", device_id, {"cause": cause, "result_at": time.time()})


# ---------------------------------------------------------------- robot reports


RECONCILE_FALLBACK_S = 3.0   # no selection report this long after connect -> send an unapplied selection
_reconcile_pending: set[str] = set()   # robots whose post-connect selection report has not arrived


def on_robot_connected(device_id: str) -> None:
    """Reconnect rule (ROBOT-STATE v2 point 11): the selection is reconciled from the robot's first
    report after connect (status selected_dog_id + selected_dog_changed_locally, see _reconcile).
    A robot that reports nothing within RECONCILE_FALLBACK_S gets a stored, unapplied selection once.
    Never a null nobody chose: a robot the relay has no selection for keeps its own (migration)."""
    _first_status_pending.add(device_id)
    _reconcile_pending.add(device_id)
    update(device_id, source="robot", reason="robot_connected", robot_online=True)
    st = get(device_id)
    if not st["selected_dog_applied"] and owns_selection(st):
        _spawn(_fallback_select(device_id))


async def _fallback_select(device_id: str) -> None:
    await asyncio.sleep(RECONCILE_FALLBACK_S)
    from app.connection_manager import get_connection_manager
    st = get(device_id)
    if (device_id in _reconcile_pending and get_connection_manager().is_robot_online(device_id)
            and owns_selection(st) and not st["selected_dog_applied"]):
        _reconcile_pending.discard(device_id)
        logger.info(f"[STATE] {device_id}: no selection report after connect; sending the stored one")
        await send_select(device_id)


def _reconcile(device_id: str, reported: Any, changed_locally: bool) -> None:
    """The robot's first selection report after a connect, once the relay owns the selection.
    a) changed locally, and the dog is null or the owner's -> adopt it (+1, source robot, reason
       local_selection), echo it back with select_dog, applied on success. Wins over a stored,
       unapplied selection.
    b) changed locally but not the owner's dog -> ignored, handled as c.
    c) not changed locally, and it differs from the stored value -> select_dog with the STORED value.
    d) the same value -> the report confirms it: selected_dog_applied true (pushed), nothing sent."""
    st = get(device_id)
    reported = reported if isinstance(reported, str) and reported else None
    if changed_locally:
        from app.database import get_dog_owner
        if reported is None or get_dog_owner(reported) == _owner(device_id):
            if reported == st["selected_dog_id"] and st["selected_dog_applied"]:
                return   # already the relay's applied value: nothing to adopt
            update(device_id, source="robot", reason="local_selection", force=True, selected_dog_id=reported,
                   selected_dog_applied=False, select_command_id=None, select_source="robot")
            logger.info(f"[STATE] {device_id}: robot's local selection {reported} adopted")
            _spawn(send_select(device_id))
            return
        logger.warning(f"[STATE] {device_id}: local selection {reported!r} is not the owner's dog; ignored")
    if reported != st["selected_dog_id"]:
        logger.info(f"[STATE] {device_id}: robot reports {reported}, stored {st['selected_dog_id']}; sending stored")
        _spawn(send_select(device_id))
    elif not st["selected_dog_applied"]:   # d) the robot already has it: its report is the confirmation
        update(device_id, source="robot", reason=st["reason"], selected_dog_applied=True, select_command_id=None)
        logger.info(f"[STATE] {device_id}: robot reports the stored selection {reported}; applied")


def on_robot_disconnected(device_id: str) -> None:
    _first_status_pending.discard(device_id)
    _reconcile_pending.discard(device_id)
    update(device_id, source="robot", reason="robot_disconnected", robot_online=False)


def owns_selection(st: dict) -> bool:
    """The relay owns selected_dog_id once it has been set by select_dog or adopted from the robot
    (select_source is set either way). Before that the robot's own value is the truth."""
    return bool(st.get("select_source"))


def _adopt_selection(device_id: str, reported: Any) -> bool:
    """Migration: a robot the relay has never owned a selection for reports the dog it has selected
    (status `selected_dog_id`, its _current_dog_id). The relay takes that value as applied and owns it
    from then on. Nothing is sent to the robot. Returns True if adopted."""
    st = get(device_id)
    if owns_selection(st):
        return False
    dog_id = reported if isinstance(reported, str) and reported else None
    if dog_id is not None:
        from app.database import get_dog_owner
        if get_dog_owner(dog_id) != _owner(device_id):
            logger.warning(f"[STATE] {device_id}: robot reports selected dog {dog_id!r}, not one of the "
                           "owner's dogs; not adopted")
            return False
    update(device_id, source="robot", reason="adopted_from_robot", selected_dog_id=dog_id,
           selected_dog_applied=True, select_source="robot")
    logger.info(f"[STATE] {device_id}: adopted the robot's selected dog {dog_id}; the relay owns it now")
    return True


def on_status(device_id: str, data: Any) -> None:
    """A status frame / event. Its mode is used once after a (re)connect; asleep whenever sent;
    selected_dog_id only until the relay owns the selection (migration)."""
    if not isinstance(data, dict):
        return
    if "selected_dog_id" in data:
        if not owns_selection(get(device_id)):
            _reconcile_pending.discard(device_id)
            _adopt_selection(device_id, data["selected_dog_id"])      # migration: first ever report
        elif device_id in _reconcile_pending:
            _reconcile_pending.discard(device_id)
            _reconcile(device_id, data["selected_dog_id"], data.get("selected_dog_changed_locally") is True)
    changes: dict[str, Any] = {}
    if isinstance(data.get("asleep"), bool):
        changes["asleep"] = data["asleep"]
    if device_id in _first_status_pending and isinstance(data.get("mode"), str):
        _first_status_pending.discard(device_id)
        mode = normalize_mode(data["mode"])
        changes["mode"] = mode
        if mode in SESSION_MODES and "session_dog_id" in data:
            changes["session_dog_id"] = data["session_dog_id"] or None
        elif mode != get(device_id)["mode"]:
            changes["session_dog_id"] = None
    if changes:
        update(device_id, source="robot", reason="status", **changes)


def on_mode_change(device_id: str, data: dict) -> None:
    """E1 mode_change from the robot (data.mode already validated as one of the five)."""
    _first_status_pending.discard(device_id)
    mode = data["mode"]
    src = data.get("source") if data.get("source") in SOURCES else "robot"
    changes: dict[str, Any] = {"mode": mode,
                               "session_dog_id": (data.get("session_dog_id") or None) if mode in SESSION_MODES else None}
    if mode != "idle":
        changes["asleep"] = False
    update(device_id, source=src, reason=data.get("reason"), **changes)


def on_command_result(device_id: str, meta: dict, message: dict) -> None:
    """The robot's command_result for C1 / C2 / C3. Only success=true changes state."""
    command = meta.get("command")
    if command not in STATE_COMMANDS or message.get("success") is not True:
        return
    res = message.get("result") if isinstance(message.get("result"), dict) else {}
    src, reason = meta.get("source") or "relay", meta.get("reason")
    if command == "set_mode":
        mode = valid_mode(res.get("mode"))
        if mode is None:
            logger.warning(f"[STATE] {device_id}: set_mode result without a valid mode: {res.get('mode')!r}")
            return
        _first_status_pending.discard(device_id)
        update(device_id, source=src, reason=reason, mode=mode, asleep=False,
               session_dog_id=res.get("session_dog_id") or None)
    elif command == "set_sleep":
        if isinstance(res.get("asleep"), bool):
            update(device_id, source=src, reason=reason, asleep=res["asleep"], mode=normalize_mode(res.get("mode")))
        # Wake greeting: only an explicit wake (app / connector) that succeeded; never the relay's
        # wake-first (ensure_awake does not mark it), never select_dog / download_song.
        if meta.get("explicit_wake") and meta.get("asleep") is False and res.get("asleep") is not True:
            _greet_on_wake(device_id, reason or src)
    elif command == "select_dog":
        st = get(device_id)
        if message.get("command_id") == st["select_command_id"] and res.get("selected_dog_id") == st["selected_dog_id"]:
            # the confirmation keeps the selection's own reason (e.g. local_selection)
            update(device_id, source=src, reason=st["reason"], selected_dog_applied=True, select_command_id=None)


# ---------------------------------------------------------------- select_dog


async def select_dog(device_id: str, dog_id: Optional[str], source: str) -> dict:
    """Store the selection (last write wins), answer at once, send C2 in the background if online."""
    cur = get(device_id)
    if cur["selected_dog_id"] == dog_id and cur["selected_dog_applied"]:
        return public(cur)
    st, _ = update(device_id, source=source, reason=None, selected_dog_id=dog_id,
                   selected_dog_applied=False, select_command_id=None, select_source=source)
    from app.connection_manager import get_connection_manager
    if get_connection_manager().is_robot_online(device_id):
        _spawn(send_select(device_id))
    return public(st)


async def send_select(device_id: str) -> Optional[dict]:
    """Send C2 once for the stored selection and wait up to 2000 ms. The WS handler applies the
    result; a timeout or offline robot leaves selected_dog_applied false (sent again on next connect)."""
    from app.connection_manager import get_connection_manager
    from app.services import command_results

    st = get(device_id)
    cid = str(uuid.uuid4())
    src = st.get("select_source") or "relay"
    update(device_id, source=src, select_command_id=cid)
    msg = {"type": "command", "command": "select_dog", "command_id": cid,
           "data": {"dog_id": st["selected_dog_id"], "source": src}}
    command_results.register(cid, device_id=device_id, command="select_dog", source=src)
    from app.services import sleep_wake
    err = await sleep_wake.send_awake(device_id, msg, source=src)   # wake-first like every command
    if err:
        logger.info(f"[STATE] {device_id}: select_dog not sent ({err}); sent on next connect")
        return None
    res = await command_results.wait(cid, COMMAND_TIMEOUT_S)
    if res is None:
        logger.warning(f"[STATE] {device_id}: select_dog {cid} timed out; sent again on next connect")
    elif not res.get("success"):
        logger.warning(f"[STATE] {device_id}: select_dog {cid} failed: {res.get('error')}")
    return res


# ---------------------------------------------------------------- app WebSocket commands


def caller_reason(message: dict) -> Optional[str]:
    """data.reason, else whatever the caller put in source (top level or data): callers never set
    the source, so their value is kept as the reason."""
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    for raw in (data.get("reason"), message.get("reason"), data.get("source"), message.get("source")):
        if raw is not None:
            return str(raw)[:120]
    return None


def stamp_app_frame(message: dict) -> None:
    """Every app command frame: data.source = "app" (set by the relay), the caller's own source value
    moved to data.reason. Frames that carry no data object (older top-level commands such as drive
    frames) get the same two keys at the top level, so their parameters are not moved."""
    reason = caller_reason(message)
    if isinstance(message.get("data"), dict):
        message.pop("source", None)
        message.pop("reason", None)
        message["data"]["source"] = "app"
        if reason is not None:
            message["data"]["reason"] = reason
    else:
        message["source"] = "app"
        if reason is not None:
            message["reason"] = reason


def command_result_frame(device_id: str, command: Optional[str], command_id: Optional[str], error: str,
                         duration_ms: int = 0) -> dict:
    """A relay-made failure in the robot's command_result shape (offline / timeout / invalid)."""
    return {"type": "command_result", "command_id": command_id, "command": command, "device_id": device_id,
            "success": False, "result": None, "error": error, "duration_ms": duration_ms}


def prepare_app_state_command(user_id: str, device_id: str, message: dict) -> Optional[str]:
    """Rewrite an app set_mode / set_sleep frame into C1 / C3 in place (source "app", the app's reason,
    a command_id) and register it. Returns an error string if it must be refused, else None."""
    from app.services import command_results

    command = message.get("command")
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    reason = caller_reason(message)
    message.pop("source", None)    # the caller's value is now the reason; the relay sets data.source
    message.pop("reason", None)
    if command == "set_mode":
        mode = valid_mode(data.get("mode"))
        if mode is None:
            return ERR_INVALID_MODE
        dog_id = data.get("dog_id") or None
        if dog_id is not None and not _owns_dog(user_id, dog_id):
            return "unknown_dog"
        payload: dict[str, Any] = {"mode": mode, "dog_id": dog_id, "source": "app", "reason": reason}
        if "timestamp" in data:
            payload["timestamp"] = data["timestamp"]
    else:  # set_sleep
        if not isinstance(data.get("asleep"), bool):
            return "invalid_asleep"
        payload = {"asleep": data["asleep"], "source": "app", "reason": reason}
    message["data"] = payload
    message["command_id"] = str(message.get("command_id") or uuid.uuid4())
    command_results.register(message["command_id"], device_id=device_id, command=command, source="app",
                             reason=reason)
    if command == "set_sleep":
        command_results._meta[message["command_id"]]["asleep"] = payload["asleep"]
        if payload["asleep"] is False:
            mark_explicit_wake(message["command_id"], device_id, "app")
    return None


async def watch_app_command(device_id: str, command: str, command_id: str, send: Callable) -> None:
    """After C1 / C3 went out for the app: no command_result in its budget (2000 ms; set_sleep false
    6000 ms) -> robot_timeout to the app. A late result is still applied to the state (and pushed)."""
    from app.services import command_results
    from app.services.sleep_wake import WAKE_TIMEOUT_S
    meta = command_results._meta.get(command_id) or {}
    budget = WAKE_TIMEOUT_S if command == "set_sleep" and meta.get("asleep") is False else COMMAND_TIMEOUT_S
    if await command_results.wait(command_id, budget) is None:
        logger.warning(f"[STATE] {device_id}: {command} {command_id} timed out")
        try:
            await send(command_result_frame(device_id, command, command_id, ERR_TIMEOUT, int(budget * 1000)))
        except Exception:  # pragma: no cover
            pass


def _owns_dog(user_id: str, dog_id: str) -> bool:
    from app.database import get_dog_owner
    return get_dog_owner(dog_id) == user_id
