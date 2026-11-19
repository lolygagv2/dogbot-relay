"""Agent-initiated speech: boot greeting, Silent Guardian re-arm, low hopper.

The boot greeting fires on power-on (`robot_connected`) and on wake from sleep (`robot_woke`,
2026-11-19) the same way: same delay, quiet-hours rule and one shared 10-minute debounce, so a
robot that boots and is woken straight away greets once.

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

DEBOUNCE_S = {"robot_connected": 600, "robot_woke": 600, "low_hopper": 6 * 3600, "sg_rearm": 60}
GREETING_EVENTS = {"robot_connected", "robot_woke"}
_seen_ids: "OrderedDict[str, float]" = OrderedDict()
_last_spoken: dict[tuple[str, str], float] = {}


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


async def handle(hook: dict) -> dict:
    """Process one relay hook. Never raises."""
    event = str(hook.get("event") or "")
    event_id = str(hook.get("event_id") or f"{event}:{time.time()}")
    device_id = hook.get("device_id")
    user_id = hook.get("user_id") or (get_dog_owner(hook["dog_id"]) if hook.get("dog_id") else None)
    if _seen(event_id):
        return {"skipped": "duplicate"}
    if event not in DEBOUNCE_S:
        return {"skipped": "unknown_event"}
    if not user_id:
        return {"skipped": "no_user"}
    # ROBOT-STATE v2 section 9: the only dog named is the robot's selected_dog_id; no selection ->
    # no dog name and no recap. No other dog is ever looked up.
    named = _selected_dog(user_id, device_id)
    dog_id = (named.get("id") or named.get("dog_id")) if named else None
    dog_name = (named.get("name") or "your dog") if named else None
    robot = _speaking_robot(user_id, device_id)
    if not dog_id and not robot:
        return {"skipped": "no_robot"}
    if _debounced("robot_connected" if event in GREETING_EVENTS else event, f"{device_id or robot or dog_id}"):
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

    if event in GREETING_EVENTS:
        if quiet_active:
            return {"skipped": "quiet_hours"}
        await asyncio.sleep(s.agent_boot_greeting_delay_seconds)
        named = _selected_dog(user_id, device_id)   # re-read: the owner may have switched dogs meanwhile
        part = "morning" if now_local.hour < 12 else "afternoon" if now_local.hour < 18 else "evening"
        if named:
            dog_id, dog_name = named.get("id") or dog_id, named.get("name") or "your dog"
            try:   # the selected dog's yesterday, in the dog's own timezone
                rollup = await client.get_summary(dog_id, "yesterday")
            except (RelayRefusal, RelayError):
                rollup = {}
            slim = {k: rollup.get(k) for k in ("date", "counts", "totals", "time_to_settle") if k in rollup}
            for ep in rollup.get("top_episodes", [])[:1]:
                slim["longest_episode"] = {"duration_s": ep.get("duration_s"), "bark_count": ep.get("bark_count"),
                                           "settle_s": ep.get("settle_s")}
            intent = BOOT_GREETING_INTENT.format(dog_name=dog_name, local_time=now_local.strftime("%A %H:%M"),
                                                 date=rollup.get("date", "yesterday"), rollup=slim)
            text = await get_brain().speak_event(intent, [slim])
            if not text:
                text = f"Good {part}. I'm back on watch with {dog_name}."
        else:   # no dog selected: no dog name, no recap
            dog_id = None
            text = await get_brain().speak_event(
                BOOT_GREETING_NO_DOG_INTENT.format(local_time=now_local.strftime("%A %H:%M")), [])
            if not text:
                text = f"Good {part}. I'm back on watch."
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
        out = await say(user_id, dog_id, text, device_id=device_id or (None if dog_id else robot))
    except Exception as e:
        logger.error(f"[AGENT] speech delivery failed: {e}")
        return {"event": event, "text": text, "error": str(e)[:120]}
    logger.info(f"[AGENT] {event} -> {dog_id}: {text!r} delivered={out.get('delivered')}")
    return {"event": event, "dog_id": dog_id, "text": text, **out}
