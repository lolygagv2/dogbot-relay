"""Robot sleep / wake (2026-11-19; ROBOT-STATE v2).

Sleep is not a mode. The relay sends C3 (same envelope as every other command):
  {"type": "command", "command": "set_sleep", "command_id",
   "data": {"asleep": true|false, "source", "reason"}}
and the robot answers within 2000 ms with command_result {success, result: {asleep, mode}}.
`asleep` lives in the robot_state record (app/services/robot_state.py); this module only does
wake-first:

- EVERY command (any "type": "command" frame except set_sleep itself) to a robot whose state says
  asleep is held, set_sleep {asleep: false} is sent, and the command goes out once the robot
  confirms. The wake gets WAKE_TIMEOUT_S (6000 ms); not confirmed -> robot_timeout (or the robot's
  own error, e.g. "expired") and the command is never sent. Nothing is queued or retried.
  Concurrent commands share one wake. Drive frames (motor/servo/pan_tilt, 20 Hz) are not held:
  they start the wake and drop, because a drive frame held for seconds would be stale.
- The wake greeting fires from robot_state when asleep goes true -> false.

In-process state (single relay worker), like command_results.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

WAKE_TIMEOUT_S = 6.0   # set_sleep {asleep: false} is confirmed within 6000 ms (every other command: 2000)
NO_WAKE_COMMANDS = {"set_sleep"}   # the wake itself, and going to sleep, never wake the robot first
DRIVE_COMMANDS = {"motor", "servo", "pan_tilt"}


@dataclass
class _Dev:
    wake_summary: Optional[asyncio.Future] = None   # ensure_awake's result, for commands that join
    held: list = field(default_factory=list)   # tasks holding commands for this wake


_state: dict[str, _Dev] = {}


def _dev(device_id: str) -> _Dev:
    return _state.setdefault(device_id, _Dev())


def is_asleep(device_id: Optional[str]) -> bool:
    if not device_id:
        return False
    from app.services import robot_state
    return robot_state.get(device_id)["asleep"]


def is_waking(device_id: Optional[str]) -> bool:
    d = _state.get(device_id or "")
    return bool(d and d.wake_summary and not d.wake_summary.done())


def reset() -> None:
    """Tests only."""
    _state.clear()


def needs_wake(device_id: Optional[str], command: Optional[str]) -> bool:
    """Every command except set_sleep, while the state says asleep (or a wake is in flight)."""
    return (bool(device_id) and bool(command) and command not in NO_WAKE_COMMANDS
            and (is_asleep(device_id) or is_waking(device_id)))


def restamp(message: dict) -> dict:
    """A held command is re-timestamped when it finally goes out, so the robot's own stale check
    (2 s, like the relay's) doesn't drop it. Keeps the original epoch-ms vs ISO format."""
    now_ms = int(time.time() * 1000)
    for holder in (message, message.get("data") if isinstance(message.get("data"), dict) else None):
        if holder is not None and "timestamp" in holder:
            holder["timestamp"] = now_ms if isinstance(holder["timestamp"], (int, float)) else \
                datetime.now(timezone.utc).isoformat()
    return message


async def ensure_awake(device_id: str, reason: str = "", source: str = "relay") -> dict:
    """Wake a sleeping robot (set_sleep {asleep: false}) and wait up to 6000 ms for its confirmation.

    Returns {woke, ready, command_id?, timed_out?, error?}. Never raises. error is
    robot_offline / robot_timeout / the robot's own error."""
    d = _dev(device_id)
    if d.wake_summary and not d.wake_summary.done():   # share the wake already in flight
        return dict(await asyncio.shield(d.wake_summary))
    if not is_asleep(device_id):
        return {"woke": False, "ready": True}

    from app.connection_manager import get_connection_manager
    from app.services import command_results, robot_state

    summary: asyncio.Future = asyncio.get_running_loop().create_future()
    d.wake_summary = summary
    cid = str(uuid.uuid4())
    src = robot_state.source_for(source)
    reason = (reason or "wake")[:64]
    msg = {"type": "command", "command": "set_sleep", "command_id": cid,
           "data": {"asleep": False, "source": src, "reason": reason}}
    command_results.register(cid, device_id=device_id, command="set_sleep", source=src, reason=reason)
    out: dict[str, Any] = {"command_id": cid, "woke": False, "ready": False}
    try:
        if not await get_connection_manager().send_to_robot(device_id, msg):
            out["error"] = robot_state.ERR_OFFLINE
            return out
        logger.info(f"[SLEEP] {device_id}: set_sleep false sent ({reason}), waiting for the robot")
        res = await command_results.wait(cid, WAKE_TIMEOUT_S)
        if res is None:
            out.update(timed_out=True, error=robot_state.ERR_TIMEOUT)
        elif not res.get("success") or is_asleep(device_id):
            out["error"] = str(res.get("error") or "not_awake")[:64]
        else:
            out.update(woke=True, ready=True)
        return out
    finally:
        if not summary.done():
            summary.set_result(out)


async def send_awake(device_id: str, message: dict, source: str = "relay") -> Optional[str]:
    """A relay-made command (select_dog, download_song): wake first if asleep, then send.
    Returns None when sent, else the error (robot_offline / robot_timeout / the robot's error)."""
    from app.connection_manager import get_connection_manager
    if needs_wake(device_id, message.get("command")):
        res = await ensure_awake(device_id, reason=str(message.get("command")), source=source)
        if not res.get("ready"):
            logger.warning(f"[SLEEP] {device_id}: {message.get('command')} not sent ({res.get('error')})")
            return res.get("error") or "not_awake"
        restamp(message)
    return None if await get_connection_manager().send_to_robot(device_id, message) else "robot_offline"


def hold_until_awake(device_id: str, message: dict, reason: str, send: Callable,
                     on_fail: Optional[Callable] = None) -> None:
    """Background: wake, then `await send(message)` (re-timestamped). For the app WebSocket path,
    which must not block its read loop for the wake. A failed wake drops the command and calls
    `await on_fail(error)`; nothing is kept for later."""
    async def _run():
        res = await ensure_awake(device_id, reason=reason, source="app")
        if not res.get("ready"):
            logger.warning(f"[SLEEP] {device_id}: dropped held {message.get('command')} ({res.get('error')})")
            if on_fail:
                await on_fail(res.get("error") or "not_awake")
            return
        await send(restamp(message))
    d = _dev(device_id)
    task = asyncio.get_running_loop().create_task(_run())
    d.held.append(task)
    task.add_done_callback(lambda t: d.held.remove(t) if t in d.held else None)
