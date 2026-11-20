"""Agent-initiated speech: boot greeting, Silent Guardian re-arm, low hopper.

The boot greeting fires on power-on (`robot_connected`: 20 s delay, 10-minute debounce). The wake
greeting (`robot_woke`, 2026-11-19) fires only for an explicit Wake from the app or a connector:
the relay sends the hook when that wake's command_result says success, and play_tts goes out
within 1000 ms. That is too short to write and record a line, so the greeting is prepared ahead
(`greeting_prepare`: when the robot goes to sleep, when the selection changes while asleep, when
the explicit wake is sent, and every PREPARE_EVERY_S for sleeping robots). Same content and
quiet-hours rule as power-on: the selected dog's name and yesterday's recap, none with no dog
selected. Not twice: a wake greeting is skipped within GREETING_GAP_S of a power-on greeting sent,
and the power-on greeting is skipped within GREETING_GAP_S of a wake greeting sent.

Hooks arrive from the relay at POST /agent/hooks/robot-event (loopback + internal token).
Each is debounced, checked against quiet hours, spoken through one no-tool Claude call,
synthesised, and pushed to the robot as a `play_tts` command through the relay.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.database import get_dog_by_id, get_dog_owner, get_user_dogs
from app.services.relay_client import RelayClient, RelayError, RelayRefusal
from app.voice_agent import tts
from app.voice_agent.brain import get_brain
from app.voice_agent.prompts import BOOT_GREETING_INTENT, BOOT_GREETING_NO_DOG_INTENT, LOW_HOPPER_INTENT, SG_REARM_INTENT

logger = logging.getLogger(__name__)

DEBOUNCE_S = {"robot_connected": 600, "low_hopper": 6 * 3600, "sg_rearm": 60}
GREETING_EVENTS = {"robot_connected", "robot_woke"}
GREETING_GAP_S = 60          # no wake greeting within 60 s of a power-on greeting sent (and vice versa)
WAKE_SEND_BUDGET_MS = 1000   # play_tts within 1000 ms of the wake's command_result
PREPARE_EVERY_S = 300
_seen_ids: "OrderedDict[str, float]" = OrderedDict()
_last_spoken: dict[tuple[str, str], float] = {}
_greeting_sent: dict[tuple[str, str], float] = {}   # (device, "boot" | "wake") -> time the play_tts went out
_prepared: dict[str, dict] = {}                     # device -> {key, dog_id, text, clip_id, url}
_preparing: dict[str, "asyncio.Task"] = {}


def _seen(event_id: str) -> bool:
    now = time.time()
    while _seen_ids and next(iter(_seen_ids.values())) < now - 3600:
        _seen_ids.popitem(last=False)
    if event_id in _seen_ids:
        return True
    _seen_ids[event_id] = now
    return False


def _debounced(event: str, key: str) -> bool:
    now = time.time()
    last = _last_spoken.get((event, key), 0)
    if now - last < DEBOUNCE_S.get(event, 60):
        return True
    _last_spoken[(event, key)] = now
    return False


def _selected_dog(user_id: Optional[str], device_id: Optional[str]) -> Optional[dict]:
    """ROBOT-STATE v2 (section 9): the only dog a spoken line may name is the robot's
    selected_dog_id (the dog chosen in the app). No device given (a replay hook): the first of the
    owner's robots with a selection. None = name no dog and give no recap."""
    from app.database import get_user_paired_devices
    from app.services import robot_state
    devices = [device_id] if device_id else (get_user_paired_devices(user_id) if user_id else [])
    for d in devices:
        sel = robot_state.get(d)["selected_dog_id"]
        if sel:
            return get_dog_by_id(sel)
    return None


def _speaking_robot(user_id: str, device_id: Optional[str]) -> Optional[str]:
    """The robot that speaks: the hook's, else one of the owner's robots, an online one first."""
    if device_id:
        return device_id
    from app.database import get_user_paired_devices
    from app.services import robot_state
    devices = get_user_paired_devices(user_id)
    return next((d for d in devices if robot_state.get(d)["robot_online"]), devices[0] if devices else None)


def _home_context(user_id: str) -> tuple[bool, Optional[str]]:
    """(quiet hours active, timezone) for a line that names no dog: quiet if ANY of the owner's dogs is
    in quiet hours; their timezone if they all share one, else None (the default)."""
    from app.services import settings_service
    dogs = [d.get("id") or d.get("dog_id") for d in get_user_dogs(user_id) if d.get("role", "owner") == "owner"]
    quiet = any(settings_service.in_quiet_hours(d) for d in dogs)
    zones = {settings_service.get_value(d, "timezone") for d in dogs}
    return quiet, (zones.pop() if len(zones) == 1 else None)


async def say(user_id: str, dog_id: Optional[str], text: str, device_id: Optional[str] = None,
              cache_key: Optional[str] = None) -> dict:
    """TTS + push to the robot via the relay. Returns {clip_id, url, delivered, simulated}. With no
    dog_id (no dog selected), the clip goes to `device_id` by the relay's dog-less device route."""
    clip_id, url = await tts.synthesize(text, cache_key=cache_key)
    return await push_clip(user_id, dog_id, clip_id, url, device_id=device_id)


async def push_clip(user_id: str, dog_id: Optional[str], clip_id: str, url: str,
                    device_id: Optional[str] = None) -> dict:
    """play_tts of an already recorded clip (the dog's route, else the dog-less device route)."""
    client = RelayClient(actor="agent", user_id=user_id)
    data = {"url": url, "clip_id": clip_id}
    try:
        if dog_id:
            res = await client.command(dog_id, "play_tts", data, device_id=device_id)
        elif device_id:
            res = await client.device_command(device_id, "play_tts", data)
        else:
            return {"clip_id": clip_id, "url": url, "delivered": False, "refused": "no_paired_device"}
        return {"clip_id": clip_id, "url": url, "delivered": res.get("delivered"), "simulated": res.get("simulated")}
    except RelayRefusal as e:
        logger.warning(f"[AGENT] play_tts refused: {e.code}")
        return {"clip_id": clip_id, "url": url, "delivered": False, "refused": e.code}


def _part_of_day(now_local: datetime) -> str:
    return "morning" if now_local.hour < 12 else "afternoon" if now_local.hour < 18 else "evening"


def _greeting_context(user_id: str, device_id: Optional[str]) -> tuple[Optional[dict], bool, datetime]:
    """(selected dog or None, quiet hours active, local now) read straight from the DB: no relay
    round trip, so the wake path stays inside its 1000 ms."""
    from app.services import settings_service
    s = get_settings()
    named = _selected_dog(user_id, device_id)
    if named:
        dog_id = named.get("id") or named.get("dog_id")
        quiet = settings_service.in_quiet_hours(dog_id)
        zone = settings_service.get_value(dog_id, "timezone")
    else:
        quiet, zone = _home_context(user_id)
    try:
        tz = ZoneInfo(zone or s.default_timezone)
    except Exception:
        tz = ZoneInfo(s.default_timezone)
    return named, quiet, datetime.now(timezone.utc).astimezone(tz)


def _greeting_key(named: Optional[dict], now_local: datetime) -> tuple:
    """A prepared greeting is good while the dog, the local day (its recap) and the part of day hold."""
    return ((named.get("id") or named.get("dog_id")) if named else None, now_local.date().isoformat(),
            _part_of_day(now_local))


async def _greeting_text(user_id: str, named: Optional[dict], now_local: datetime, phrase: str) -> str:
    """The greeting: the selected dog's name + yesterday's recap + one light joke; no dog -> neither."""
    client = RelayClient(actor="agent", user_id=user_id)
    part = _part_of_day(now_local)
    if named:
        dog_id, dog_name = named.get("id") or named.get("dog_id"), named.get("name") or "your dog"
        try:   # the selected dog's yesterday, in the dog's own timezone
            rollup = await client.get_summary(dog_id, "yesterday")
        except (RelayRefusal, RelayError):
            rollup = {}
        slim = {k: rollup.get(k) for k in ("date", "counts", "totals", "time_to_settle") if k in rollup}
        for ep in rollup.get("top_episodes", [])[:1]:
            slim["longest_episode"] = {"duration_s": ep.get("duration_s"), "bark_count": ep.get("bark_count"),
                                       "settle_s": ep.get("settle_s")}
        intent = BOOT_GREETING_INTENT.format(event_phrase=phrase, dog_name=dog_name,
                                             local_time=now_local.strftime("%A %H:%M"),
                                             date=rollup.get("date", "yesterday"), rollup=slim)
        text = await get_brain().speak_event(intent, [slim])
        return text or f"Good {part}. I'm back on watch with {dog_name}."
    text = await get_brain().speak_event(
        BOOT_GREETING_NO_DOG_INTENT.format(event_phrase=phrase, local_time=now_local.strftime("%A %H:%M")), [])
    return text or f"Good {part}. I'm back on watch."


def _fresh(device_id: str, key: tuple) -> Optional[dict]:
    p = _prepared.get(device_id)
    return p if p and p["key"] == key and tts.clip_path(p["clip_id"]) else None


async def prepare_greeting(user_id: str, device_id: str) -> Optional[dict]:
    """Write and record the wake greeting for the robot's current selection, unless a fresh one is
    ready. Concurrent calls share one build. Never raises."""
    named, _quiet, now_local = _greeting_context(user_id, device_id)
    key = _greeting_key(named, now_local)
    ready = _fresh(device_id, key)
    if ready:
        return ready
    task = _preparing.get(device_id)
    if task and not task.done():
        return await asyncio.shield(task)

    async def _build() -> Optional[dict]:
        try:
            text = await _greeting_text(user_id, named, now_local, "woke up from sleep")
            clip_id, url = await tts.synthesize(text, cache_key=f"wake_greeting:{device_id}:{text}")
        except Exception as e:
            logger.warning(f"[AGENT] wake greeting for {device_id} not prepared: {e}")
            return None
        old = _prepared.get(device_id)
        entry = {"key": key, "dog_id": key[0], "text": text, "clip_id": clip_id, "url": url}
        _prepared[device_id] = entry
        if old and old["clip_id"] != clip_id:   # canned clips are never swept: drop the replaced one
            try:
                p = tts.clip_path(old["clip_id"])
                if p:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        logger.info(f"[AGENT] wake greeting ready for {device_id} ({key[0]}): {text!r}")
        return entry

    task = asyncio.get_running_loop().create_task(_build())
    _preparing[device_id] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _preparing.get(device_id) is task:
            _preparing.pop(device_id, None)


def _recent(device_id: str, kind: str) -> bool:
    return time.time() - _greeting_sent.get((device_id, kind), 0) < GREETING_GAP_S


async def wake_greeting(user_id: str, device_id: str, hook: dict) -> dict:
    """Explicit wake confirmed: play_tts of the prepared greeting at once."""
    if _recent(device_id, "boot"):
        logger.info(f"[AGENT] robot_woke {device_id}: power-on greeting sent < {GREETING_GAP_S}s ago; skipped")
        return {"skipped": "boot_greeting_recent"}
    named, quiet, now_local = _greeting_context(user_id, device_id)
    if quiet:
        return {"skipped": "quiet_hours"}
    key = _greeting_key(named, now_local)
    entry = _fresh(device_id, key)
    late = entry is None
    if late:   # nothing ready (agent restarted, or the dog / part of day changed): record it now
        entry = await prepare_greeting(user_id, device_id)
        if not entry or entry["key"] != key:
            return {"skipped": "greeting_unavailable"}
    if _recent(device_id, "boot"):
        return {"skipped": "boot_greeting_recent"}
    _greeting_sent[(device_id, "wake")] = time.time()
    started = float((hook.get("payload") or {}).get("result_at") or time.time())
    ms = int((time.time() - started) * 1000)   # play_tts leaves now; push_clip then waits for its result
    log = logger.warning if ms > WAKE_SEND_BUDGET_MS else logger.info
    log(f"[AGENT] robot_woke {device_id}: sending play_tts {ms} ms after the wake result"
        f"{' (not prepared ahead)' if late else ''}")
    out = await push_clip(user_id, entry["dog_id"], entry["clip_id"], entry["url"], device_id=device_id)
    logger.info(f"[AGENT] robot_woke -> {entry['dog_id']}: play_tts {ms} ms after the wake result"
        f"{' (not prepared ahead)' if late else ''}: {entry['text']!r} delivered={out.get('delivered')}")
    return {"event": "robot_woke", "dog_id": entry["dog_id"], "text": entry["text"], "send_ms": ms, **out}


async def prepare_loop() -> None:
    """Keep a fresh wake greeting ready for every sleeping robot (covers agent restarts, a new day,
    a new part of day)."""
    from app.database import db_connection
    while True:
        try:
            with db_connection() as conn:
                rows = conn.execute("SELECT device_id FROM robot_state WHERE asleep = 1").fetchall()
            for (device_id,) in rows:
                from app.database import get_device_owner
                owner = get_device_owner(device_id)
                if owner:
                    await prepare_greeting(owner, device_id)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[AGENT] prepare loop: {e}")
        await asyncio.sleep(PREPARE_EVERY_S)


async def handle(hook: dict) -> dict:
    """Process one relay hook. Never raises."""
    event = str(hook.get("event") or "")
    event_id = str(hook.get("event_id") or f"{event}:{time.time()}")
    device_id = hook.get("device_id")
    user_id = hook.get("user_id") or (get_dog_owner(hook["dog_id"]) if hook.get("dog_id") else None)
    if _seen(event_id):
        return {"skipped": "duplicate"}
    if event not in DEBOUNCE_S and event not in ("robot_woke", "greeting_prepare"):
        return {"skipped": "unknown_event"}
    if not user_id:
        return {"skipped": "no_user"}
    if event in ("robot_woke", "greeting_prepare"):
        if not device_id:
            return {"skipped": "no_robot"}
        try:
            if event == "greeting_prepare":
                entry = await prepare_greeting(user_id, device_id)
                return {"event": event, "prepared": bool(entry)}
            return await wake_greeting(user_id, device_id, hook)
        except Exception as e:
            logger.error(f"[AGENT] {event} failed: {e}")
            return {"event": event, "error": str(e)[:120]}
    # ROBOT-STATE v2 section 9: the only dog named is the robot's selected_dog_id; no selection ->
    # no dog name and no recap. No other dog is ever looked up.
    named = _selected_dog(user_id, device_id)
    dog_id = (named.get("id") or named.get("dog_id")) if named else None
    dog_name = (named.get("name") or "your dog") if named else None
    robot = _speaking_robot(user_id, device_id)
    if not dog_id and not robot:
        return {"skipped": "no_robot"}
    if _debounced(event, f"{device_id or robot or dog_id}"):
        return {"skipped": "debounced"}

    s = get_settings()
    client = RelayClient(actor="agent", user_id=user_id)
    if dog_id:
        try:
            status = await client.get_status(dog_id)
        except (RelayRefusal, RelayError) as e:
            logger.warning(f"[AGENT] status unavailable for {dog_id}: {e}")
            status = {}
        quiet_active = bool((status.get("quiet_hours") or {}).get("active"))
        zone = status.get("timezone")
    else:
        quiet_active, zone = _home_context(user_id)
    tz = ZoneInfo(zone or s.default_timezone)
    now_local = datetime.now(timezone.utc).astimezone(tz)

    greeting_robot = device_id or robot
    if event == "robot_connected":
        if quiet_active:
            return {"skipped": "quiet_hours"}
        await asyncio.sleep(s.agent_boot_greeting_delay_seconds)
        if greeting_robot and _recent(greeting_robot, "wake"):
            logger.info(f"[AGENT] robot_connected {greeting_robot}: wake greeting sent < {GREETING_GAP_S}s ago; skipped")
            return {"skipped": "wake_greeting_recent"}
        named = _selected_dog(user_id, device_id)   # re-read: the owner may have switched dogs meanwhile
        dog_id = (named.get("id") or dog_id) if named else None
        text = await _greeting_text(user_id, named, now_local, "started up")
    elif event == "sg_rearm":
        payload = hook.get("payload") or {}
        if dog_name:
            text = await get_brain().speak_event(SG_REARM_INTENT.format(dog_name=dog_name, payload=payload), [payload])
        else:
            text = None
        if not text:
            text = f"Silent Guardian is back on for {dog_name}." if dog_name else "Silent Guardian is back on."
    else:  # low_hopper
        if quiet_active:
            return {"skipped": "quiet_hours"}
        payload = hook.get("payload") or {}
        text = (await get_brain().speak_event(LOW_HOPPER_INTENT.format(dog_name=dog_name, payload=payload), [payload])
                if dog_name else None)
        if not text:
            tr = payload.get("treats_remaining")
            text = "The treat hopper is running low" + (f" for {dog_name}" if dog_name else "") + \
                (f", {tr} left." if tr is not None else ".")

    try:
        if event == "robot_connected" and greeting_robot:
            if _recent(greeting_robot, "wake"):
                return {"skipped": "wake_greeting_recent"}
            _greeting_sent[(greeting_robot, "boot")] = time.time()
        out = await say(user_id, dog_id, text, device_id=device_id or (None if dog_id else robot))
    except Exception as e:
        logger.error(f"[AGENT] speech delivery failed: {e}")
        return {"event": event, "text": text, "error": str(e)[:120]}
    logger.info(f"[AGENT] {event} -> {dog_id}: {text!r} delivered={out.get('delivered')}")
    return {"event": event, "dog_id": dog_id, "text": text, **out}
