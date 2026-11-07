"""Agent-initiated speech: boot greeting, Silent Guardian re-arm, low hopper.

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
from app.voice_agent.prompts import BOOT_GREETING_INTENT, LOW_HOPPER_INTENT, SG_REARM_INTENT

logger = logging.getLogger(__name__)

DEBOUNCE_S = {"robot_connected": 600, "low_hopper": 6 * 3600, "sg_rearm": 60}
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


def _pick_dog(user_id: Optional[str], dog_id: Optional[str], device_id: Optional[str] = None) -> Optional[dict]:
    if dog_id:
        d = get_dog_by_id(dog_id)
        if d:
            return d
    if device_id:  # the robot owner's oldest dog (or placeholder)
        from app.services import pairing
        primary = pairing.primary_dog_id(device_id)
        d = get_dog_by_id(primary) if primary else None
        if d:
            return d
    if user_id:
        dogs = get_user_dogs(user_id)
        if dogs:
            d = dogs[0]
            return get_dog_by_id(d.get("id") or d.get("dog_id")) or d
    return None


async def say(user_id: str, dog_id: str, text: str, device_id: Optional[str] = None,
              cache_key: Optional[str] = None) -> dict:
    """TTS + push to the robot via the relay. Returns {clip_id, url, delivered, simulated}."""
    clip_id, url = await tts.synthesize(text, cache_key=cache_key)
    client = RelayClient(actor="agent", user_id=user_id)
    try:
        res = await client.command(dog_id, "play_tts", {"url": url, "clip_id": clip_id}, device_id=device_id)
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
    dog = _pick_dog(user_id, hook.get("dog_id"), device_id)
    if not dog:
        return {"skipped": "no_dog"}
    dog_id = dog.get("id") or dog.get("dog_id")
    dog_name = dog.get("name") or "your dog"
    if _debounced(event, f"{device_id or dog_id}"):
        return {"skipped": "debounced"}

    s = get_settings()
    client = RelayClient(actor="agent", user_id=user_id)
    try:
        status = await client.get_status(dog_id)
    except (RelayRefusal, RelayError) as e:
        logger.warning(f"[AGENT] status unavailable for {dog_id}: {e}")
        status = {}
    quiet_active = bool((status.get("quiet_hours") or {}).get("active"))
    tz = ZoneInfo(status.get("timezone") or s.default_timezone)
    now_local = datetime.now(timezone.utc).astimezone(tz)

    if event == "robot_connected":
        if quiet_active:
            return {"skipped": "quiet_hours"}
        await asyncio.sleep(s.agent_boot_greeting_delay_seconds)
        try:
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
            text = f"Good {'morning' if now_local.hour < 12 else 'afternoon' if now_local.hour < 18 else 'evening'}. I'm back on watch with {dog_name}."
    elif event == "sg_rearm":
        payload = hook.get("payload") or {}
        text = await get_brain().speak_event(SG_REARM_INTENT.format(dog_name=dog_name, payload=payload), [payload])
        if not text:
            text = f"Silent Guardian is back on for {dog_name}."
    else:  # low_hopper
        if quiet_active:
            return {"skipped": "quiet_hours"}
        payload = hook.get("payload") or {}
        text = await get_brain().speak_event(LOW_HOPPER_INTENT.format(dog_name=dog_name, payload=payload), [payload])
        if not text:
            tr = payload.get("treats_remaining")
            text = f"The treat hopper is running low for {dog_name}" + (f", {tr} left." if tr is not None else ".")

    try:
        out = await say(user_id, dog_id, text, device_id=device_id if event != "robot_connected" else device_id)
    except Exception as e:
        logger.error(f"[AGENT] speech delivery failed: {e}")
        return {"event": event, "text": text, "error": str(e)[:120]}
    logger.info(f"[AGENT] {event} -> {dog_id}: {text!r} delivered={out.get('delivered')}")
    return {"event": event, "dog_id": dog_id, "text": text, **out}
