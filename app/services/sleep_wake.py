"""Robot sleep / wake (2026-11-19).

Robot commands (relay -> robot, same envelope as every other command):
  {"type": "command", "command": "sleep", "data": {"source"}, "command_id"}
  {"type": "command", "command": "wake",  "data": {"source", "reason"}, "command_id"}
The robot answers `sleep` with command_result {ok, result: {mode: "sleep"}} (ok=false to refuse,
e.g. mid-mission) and reports mode "sleep" in its status while asleep. It answers `wake` with
command_result {ok: true, result: {ready: true, mode, was_asleep}} only once it can take commands;
an already-awake robot answers at once.

What the relay does:
- Tracks each robot's sleep state from what it sends (sleep) and what the robot reports
  (status / mode_change / command_result). Spellings "sleeping" and "asleep" count as "sleep".
- Wake-first: any action command (WAKING_COMMANDS) to a sleeping robot is held, a `wake` is sent,
  and the command goes out once the robot says it is ready (or after WAKE_READY_TIMEOUT_S, when
  the relay assumes the robot was not really asleep and sends it anyway). Concurrent commands
  share one wake. Drive frames (motor/servo/pan_tilt) are not held: they start the wake and drop.
- Stale "idle": the robot is known to report a stale idle in routine status messages, so a sleep
  the relay sent is cleared only by a wake result, an explicit mode_change, or a non-sleep mode in
  a command_result. A sleep the robot reported by itself clears on any later non-sleep report.
- On an actual wake (asleep -> awake) the voice agent gets a `robot_woke` hook and speaks the boot
  greeting exactly as on power-on (same delay, quiet-hours rule and shared 10-minute debounce).

In-process state (single relay worker), like command_results.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

SLEEP_MODE = "sleep"
SLEEP_SPELLINGS = {"sleep", "sleeping", "asleep"}
# Well under the voice agent / MCP RelayClient timeout (15 s + wait_s), so a held HTTP command
# still returns inside the caller's timeout.
WAKE_READY_TIMEOUT_S = 12.0

# Commands that need an awake robot. Everything else (config pushes, schedule_sync, smart_plug,
# uploads, profile syncs, sleep, ...) is delivered to a sleeping robot as-is.
WAKING_COMMANDS = {
    # mode starts and missions
    "set_mode", "start_mission", "run_trick", "standby",
    # physical actions
    "dispense_treat", "treat", "go_to", "move", "look", "motor", "servo", "pan_tilt",
    # camera / mic / speaker
    "take_photo", "listen", "play_sound", "play_tts", "play_music", "audio", "ultrasonic",
}
DRIVE_COMMANDS = {"motor", "servo", "pan_tilt"}


@dataclass
class _Dev:
    asleep: bool = False
    by: str = ""                 # "relay" (we sent sleep) | "robot" (the robot reported it)
    since: float = 0.0
    wake_cid: Optional[str] = None
    wake_fut: Optional[asyncio.Future] = None       # the robot's answer to the wake in flight
    wake_summary: Optional[asyncio.Future] = None   # ensure_awake's result, for commands that join
    held: list = field(default_factory=list)   # tasks holding commands for this wake


_state: dict[str, _Dev] = {}


def _dev(device_id: str) -> _Dev:
    return _state.setdefault(device_id, _Dev())


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_mode(mode: Any) -> Optional[str]:
    if not isinstance(mode, str) or not mode.strip():
        return None
    m = mode.strip().lower()
    return SLEEP_MODE if m in SLEEP_SPELLINGS else m


def is_asleep(device_id: Optional[str]) -> bool:
    return bool(device_id) and _dev(device_id).asleep


def is_waking(device_id: Optional[str]) -> bool:
    d = _state.get(device_id or "")
    return bool(d and d.wake_summary and not d.wake_summary.done())


def snapshot(device_id: str) -> dict:
    d = _dev(device_id)
    return {"asleep": d.asleep, "by": d.by or None, "waking": is_waking(device_id),
            "since": d.since or None}


def reset() -> None:
    """Tests only."""
    _state.clear()


# ---------------------------------------------------------------- outgoing


def translate(message: dict) -> dict:
    """An app `set_mode {mode: sleep|sleeping|asleep}` becomes the robot's `sleep` command."""
    if message.get("command") == "set_mode":
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if normalize_mode(data.get("mode") or message.get("mode")) == SLEEP_MODE:
            message["command"] = "sleep"
            message["data"] = {k: v for k, v in data.items() if k != "mode"}
            message.pop("mode", None)
    return message


def needs_wake(device_id: Optional[str], command: Optional[str]) -> bool:
    return bool(device_id) and command in WAKING_COMMANDS and (is_asleep(device_id) or is_waking(device_id))


def note_sent(device_id: str, message: dict) -> None:
    """Call after a message reached the robot."""
    if message.get("command") == "sleep":
        d = _dev(device_id)
        if not d.asleep:
            d.since = time.time()
        d.asleep, d.by = True, "relay"
        logger.info(f"[SLEEP] {device_id}: sleep sent, robot treated as asleep")


def restamp(message: dict) -> dict:
    """A held command is re-timestamped when it finally goes out, so the robot's own stale check
    (2 s, like the relay's) doesn't drop it. Keeps the original epoch-ms vs ISO format."""
    now_ms = int(time.time() * 1000)
    for holder in (message, message.get("data") if isinstance(message.get("data"), dict) else None):
        if holder is not None and "timestamp" in holder:
            holder["timestamp"] = now_ms if isinstance(holder["timestamp"], (int, float)) else _iso()
    return message


# ---------------------------------------------------------------- incoming


def on_robot_connected(device_id: str) -> None:
    """A (re)connect may be a reboot (awake) or a network blip (still asleep). Let the robot's next
    report decide: a relay-sent sleep is downgraded so any non-sleep report clears it."""
    d = _dev(device_id)
    if d.asleep:
        d.by = "robot"


def on_mode_report(device_id: str, mode: Any, explicit: bool) -> None:
    """explicit=True for mode_change / mode_changed events; False for routine status."""
    m = normalize_mode(mode)
    if not m:
        return
    d = _dev(device_id)
    if m == SLEEP_MODE:
        if not d.asleep:
            d.asleep, d.by, d.since = True, "robot", time.time()
            logger.info(f"[SLEEP] {device_id}: robot reports asleep")
        return
    if explicit and d.wake_fut and not d.wake_fut.done():
        d.wake_fut.set_result({"ok": True, "result": {"ready": True, "mode": m}, "via": "mode_change"})
        return   # ensure_awake finishes the transition
    if d.asleep and (explicit or d.by == "robot"):
        _set_awake(device_id, cause=f"mode:{m}")


def on_command_result(device_id: str, command: Optional[str], payload: dict) -> None:
    d = _dev(device_id)
    ok = bool(payload.get("ok", True))
    if command == "sleep":
        if ok:
            if not d.asleep:
                d.since = time.time()
            d.asleep, d.by = True, "relay"
        elif d.asleep and d.by == "relay":
            d.asleep, d.by = False, ""   # the robot refused to sleep
            logger.info(f"[SLEEP] {device_id}: robot refused sleep: {payload.get('error')}")
    elif command == "wake":
        cid = str(payload.get("command_id") or "")
        if d.wake_fut and not d.wake_fut.done() and (not cid or cid == d.wake_cid):
            d.wake_fut.set_result(payload)


def _set_awake(device_id: str, cause: str, greet: bool = True) -> None:
    d = _dev(device_id)
    was = d.asleep
    d.asleep, d.by, d.since = False, "", 0.0
    if was:
        logger.info(f"[SLEEP] {device_id}: awake ({cause})")
        if greet:
            _greet(device_id, cause)


def _greet(device_id: str, cause: str) -> None:
    """Boot greeting on wake: the same agent hook path as robot_connected."""
    try:
        from app.connection_manager import get_connection_manager
        from app.services.notify_service import notify_agent
        owner = get_connection_manager().get_device_owner(device_id)
        if owner:
            notify_agent("robot_woke", device_id, owner, None, {"cause": cause}, None)
    except Exception as e:  # never let a greeting break a command
        logger.debug(f"[AGENT-HOOK] robot_woke hook failed: {e}")


# ---------------------------------------------------------------- wake


async def ensure_awake(device_id: str, reason: str = "", force: bool = False,
                       source: str = "relay", command_id: Optional[str] = None) -> dict:
    """Wake the robot if it is (or may be) asleep and wait until it says it is ready.

    Returns {woke, ready, command_id?, waited_s?, timed_out?, error?, delivered?}. Never raises.
    force=True sends a wake even if the relay thinks the robot is awake (an explicit wake)."""
    d = _dev(device_id)
    if d.wake_summary and not d.wake_summary.done():   # share the wake already in flight
        return dict(await asyncio.shield(d.wake_summary))
    if not force and not d.asleep:
        return {"woke": False, "ready": True}

    from app.connection_manager import get_connection_manager
    from app.services import command_results

    loop = asyncio.get_running_loop()
    cid = command_id or str(uuid.uuid4())
    d.wake_cid, d.wake_fut = cid, loop.create_future()
    summary: asyncio.Future = loop.create_future()
    d.wake_summary = summary
    thought_asleep = d.asleep
    started = time.monotonic()
    msg = {"type": "command", "command": "wake", "command_id": cid, "device_id": device_id,
           "data": {"source": source, "reason": (reason or "owner")[:64]}, "source": source, "timestamp": _iso()}
    command_results.register(cid, device_id=device_id, command="wake")
    out: dict[str, Any] = {"command_id": cid}
    try:
        delivered = await get_connection_manager().send_to_robot(device_id, msg)
        out["delivered"] = delivered
        if not delivered:
            out.update(woke=False, ready=False, error="robot_offline")
            return out
        logger.info(f"[SLEEP] {device_id}: wake sent ({reason or 'owner'}), waiting for ready")
        try:
            res = await asyncio.wait_for(asyncio.shield(d.wake_fut), timeout=WAKE_READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            res = None
        out["waited_s"] = round(time.monotonic() - started, 2)
        if res is None:
            # No answer: an older robot without `wake`, or it never slept. Don't hold commands again.
            logger.warning(f"[SLEEP] {device_id}: no ready after {WAKE_READY_TIMEOUT_S}s; sending anyway")
            _set_awake(device_id, cause="wake_timeout", greet=False)
            out.update(woke=False, ready=False, timed_out=True)
            return out
        inner = res.get("result") if isinstance(res.get("result"), dict) else {}
        if not bool(res.get("ok", True)) or inner.get("ready") is False:
            err = res.get("error")
            out.update(woke=False, ready=False, error=(err.get("code") if isinstance(err, dict) else err) or "not_ready")
            return out
        was_asleep = inner.get("was_asleep")
        actually_woke = bool(was_asleep) if isinstance(was_asleep, bool) else thought_asleep
        d.asleep = True if actually_woke else d.asleep   # so _set_awake sees the transition
        _set_awake(device_id, cause=f"wake:{reason or 'owner'}", greet=actually_woke)
        out.update(woke=actually_woke, ready=True)
        if inner.get("mode"):
            out["mode"] = inner["mode"]
        return out
    finally:
        if not summary.done():
            summary.set_result(out)
        if not d.wake_fut.done():
            d.wake_fut.cancel()


def hold_until_awake(device_id: str, message: dict, reason: str, send) -> None:
    """Background: wake, then `await send(message)` (re-timestamped). For the app WebSocket path,
    which must not block its read loop for the wake."""
    async def _run():
        res = await ensure_awake(device_id, reason=reason)
        if res.get("error") == "robot_offline":
            logger.warning(f"[SLEEP] {device_id}: dropped held {message.get('command')} (robot offline)")
            return
        await send(restamp(message))
    d = _dev(device_id)
    task = asyncio.get_running_loop().create_task(_run())
    d.held.append(task)
    task.add_done_callback(lambda t: d.held.remove(t) if t in d.held else None)
