"""Allowlisted robot control with guards (Dec 9 demo build, 2026-11-06).

Every HTTP actor (app, admin, MCP connector, voice agent) and the relay's own
departure triggers go through `dispatch`. The outgoing robot message is built fresh
from a `match` on the command name, never copied from the request, so nothing outside
the allowlist (webrtc/live view, motor, download_song, ...) can ever be emitted.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.auth import Actor
from app.database import get_dog_owner, get_user_paired_devices
from app.models import Missions, Sounds
from app.services import settings_service

logger = logging.getLogger(__name__)

SAFE_MODES = {"idle", "silent_guardian", "coach", "mission"}  # never "manual" (drive) over HTTP
KNOWN_MISSION_TYPES = {"recall", "sit_stay", "find_treat", "place", "quiet", "down_stay", "spin", "sit", "down", "stay"}
KNOWN_TRICKS = {"sit", "down", "stay", "spin", "place", "paw", "speak"}
KNOWN_TARGETS = {"dock", "bed", "kitchen", "door", "crate", "living_room", "owner"}

# What each actor kind may ask for. "relay" = departure triggers (not reachable over HTTP).
ACTOR_ALLOWED: dict[str, set[str]] = {
    "app":       {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "take_photo", "standby", "run_trick"},
    "admin":     {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "take_photo", "standby", "run_trick"},
    "agent":     {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "take_photo", "standby", "run_trick", "play_tts"},
    # PHOTO ACCESS (2026-11-06): connectors may take a still (camera scope, enforced by the MCP
    # server) — never with HUD, max one per 30 s per assistant. (Quiet hours no longer apply:
    # since 2026-11-07 they suppress only music, proactive speech and non-urgent pushes.)
    "connector": {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "standby", "run_trick", "take_photo"},
    # play_tts for "relay" = speak(): the URL is always a relay-made WIM-Z voice clip.
    "relay":     {"start_mission", "play_music", "play_tts"},
}

PHOTO_MIN_INTERVAL_S = 30.0
_last_photo: dict[str, float] = {}   # actor label -> monotonic time of last connector snapshot

# Belt and braces: these can never be the outgoing command name.
NEVER_EMIT = {
    "webrtc_request", "webrtc_offer", "webrtc_answer", "webrtc_ice", "webrtc_close", "webrtc_credentials",
    "camera_on", "camera_off", "start_stream", "stop_stream", "start_live_view", "video_start", "record_start",
    "download_song", "motor", "servo", "pan_tilt", "drive", "set_led", "led",
}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_robot_message(command: str, data: dict, actor: Actor, dog_id: str) -> tuple[dict, dict]:
    """Return (robot_message, guard_info). Raises HTTPException on denial."""
    allowed = ACTOR_ALLOWED.get(actor.kind, set())
    if command not in allowed:
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "command": command,
                                                     "actor": actor.kind})
    data = data if isinstance(data, dict) else {}
    command_id = str(uuid.uuid4())
    guard: dict[str, Any] = {}
    source = actor.kind

    match command:
        case "dispense_treat":
            guard = settings_service.check_treat_cap(dog_id, actor)
            command_id = guard["command_id"]
            out = ("dispense_treat", {"source": source, "command_id": command_id,
                                      "multiplier": guard["multiplier"], "dog_id": dog_id})
        case "standby":
            out = ("set_mode", {"mode": "idle", "source": source, "timestamp": _iso()})
        case "set_mode":
            mode = str(data.get("mode") or "")
            if mode not in SAFE_MODES:
                raise HTTPException(status_code=403, detail={"code": "mode_not_allowed", "mode": mode,
                                                             "allowed": sorted(SAFE_MODES)})
            out = ("set_mode", {"mode": mode, "source": source, "timestamp": _iso()})
        case "play_sound":
            sound_id = str(data.get("sound_id") or "")
            sounds = Sounds.model_validate(settings_service.get_value(dog_id, "sounds"))
            match_s = next((s for s in sounds.sounds if s.id == sound_id), None)
            if not match_s:
                raise HTTPException(status_code=404, detail={"code": "sound_not_found", "sound_id": sound_id})
            out = ("audio", {"file": match_s.file, "sound_id": match_s.id,
                             "url": match_s.url or settings_service.sound_url(dog_id, match_s.id),
                             "source": source})
        case "play_tts":
            url = str(data.get("url") or "")
            if not url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
                raise HTTPException(status_code=422, detail={"code": "bad_tts_url"})
            out = ("play_sound", {"url": url, "clip_id": str(data.get("clip_id") or "")[:64], "kind": "tts",
                                  "source": source})
        case "play_music":
            if settings_service.in_quiet_hours(dog_id):
                raise HTTPException(status_code=403, detail={"code": "guard_denied", "guard": "music_quiet_hours",
                                                             "reason": "no music during quiet hours"})
            track = str(data.get("track") or "")[:64]
            sounds = Sounds.model_validate(settings_service.get_value(dog_id, "sounds"))
            song = next((s for s in sounds.sounds if s.id == track), None)
            out = ("play_music", {"track": track, "file": song.file if song else None,
                                  "url": (song.url or settings_service.sound_url(dog_id, song.id)) if song else None,
                                  "duration_min": int(data.get("duration_min") or 30),
                                  "volume": int(data.get("volume") or 40), "trigger": data.get("trigger"),
                                  "source": source})
        case "start_mission":
            missions = Missions.model_validate(settings_service.get_value(dog_id, "missions"))
            mid = data.get("mission_id")
            mtype = data.get("mission_type")
            m = next((x for x in missions.missions if x.id == mid), None) if mid else None
            if m is None and mtype not in KNOWN_MISSION_TYPES:
                raise HTTPException(status_code=404, detail={"code": "mission_not_found", "mission_id": mid,
                                                             "mission_type": mtype})
            out = ("start_mission", {"mission_id": m.id if m else None, "mission_type": m.type if m else mtype,
                                     "dog_id": dog_id, "trigger": data.get("trigger") or source,
                                     "duration_s": m.duration_s if m else None,
                                     "reward_treats": m.reward_treats if m else None})
        case "take_photo":
            with_hud = bool(data.get("with_hud", False))
            if actor.kind == "connector":
                with_hud = False
                last = _last_photo.get(actor.label, 0.0)
                now_m = time.monotonic()
                if now_m - last < PHOTO_MIN_INTERVAL_S:
                    raise HTTPException(status_code=429, detail={"code": "camera_rate",
                                                                 "retry_after_s": int(PHOTO_MIN_INTERVAL_S - (now_m - last)) + 1})
                _last_photo[actor.label] = now_m
            out = ("take_photo", {"with_hud": with_hud, "source": source, "command_id": command_id})
        case "go_to":
            target = str(data.get("target") or "")[:64]
            if target not in KNOWN_TARGETS:
                raise HTTPException(status_code=422, detail={"code": "unknown_target", "target": target,
                                                             "allowed": sorted(KNOWN_TARGETS)})
            out = ("go_to", {"target": target, "source": source})
        case "run_trick":
            trick = str(data.get("trick") or "")[:64]
            if trick not in KNOWN_TRICKS:
                raise HTTPException(status_code=422, detail={"code": "unknown_trick", "trick": trick,
                                                             "allowed": sorted(KNOWN_TRICKS)})
            out = ("run_trick", {"trick": trick, "source": source})
        case _:
            raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "command": command})

    name, payload = out
    assert name not in NEVER_EMIT
    payload = {k: v for k, v in payload.items() if v is not None}
    message = {
        "type": "command",
        "command": name,
        "data": payload,
        "command_id": command_id,
        "source": actor.label,
        "timestamp": _iso(),
    }
    return message, guard


def _pick_device(owner: Optional[str], requested: Optional[str],
                 dog_id: Optional[str] = None) -> tuple[Optional[str], bool]:
    """(device_id, online). Requested device must belong to the owner. Otherwise any of the
    owner's robots, an online one first. Robots aren't tied to dogs, so dog_id doesn't narrow it."""
    from app.connection_manager import get_connection_manager

    manager = get_connection_manager()
    devices = get_user_paired_devices(owner) if owner else []
    if requested:
        if requested not in devices:
            raise HTTPException(status_code=404, detail={"code": "device_not_paired", "device_id": requested})
        return requested, manager.is_robot_online(requested)
    for d in devices:
        if manager.is_robot_online(d):
            return d, True
    return (devices[0] if devices else None), False


async def dispatch(dog_id: str, command: str, data: dict, actor: Actor,
                   device_id: Optional[str] = None, allow_relay: bool = False,
                   wait_s: Optional[float] = None) -> dict:
    """Guard, build and deliver one robot command. Returns the 202 body.

    With `wait_s`, waits for the robot's `command_result` (amendment 2026-11-06) and adds
    `result` (or `timed_out: true`)."""
    from app.services import command_results

    if actor.kind == "relay" and not allow_relay:
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed"})
    message, guard = build_robot_message(command, data, actor, dog_id)
    owner = get_dog_owner(dog_id)
    target, online = _pick_device(owner, device_id, dog_id)

    # Demo dog with no live robot -> let the replayer simulate the effect.
    if not online:
        try:
            from app.services.replayer import get_replayer
            rp = get_replayer()
            if rp.handles(dog_id):
                await rp.simulate_command(dog_id, message, actor)
                return {"command_id": message["command_id"], "device_id": target or rp.device_id,
                        "command_sent": message["command"], "delivered": False, "simulated": True,
                        "guard": guard}
        except ImportError:
            pass
        if not target:
            raise HTTPException(status_code=404, detail={"code": "no_paired_device"})
        raise HTTPException(status_code=409, detail={"code": "robot_offline", "device_id": target})

    from app.connection_manager import get_connection_manager
    message["device_id"] = target
    command_results.register(message["command_id"], dog_id=dog_id, device_id=target, command=message["command"])
    delivered = await get_connection_manager().send_to_robot(target, message)
    if not delivered:
        raise HTTPException(status_code=409, detail={"code": "robot_offline", "device_id": target})
    logger.info(f"[CONTROL] {actor.label} -> {target}: {message['command']} ({message['command_id']})")
    out = {"command_id": message["command_id"], "device_id": target, "command_sent": message["command"],
           "delivered": True, "simulated": False, "guard": guard}
    if wait_s:
        res = await command_results.wait(message["command_id"], float(wait_s))
        if res is None:
            out["timed_out"] = True
            out["result"] = None
        else:
            out["result"] = {k: res.get(k) for k in ("ok", "result", "error", "timestamp")}
    return out


# ---------------------------------------------------------------------------
# speak (2026-11-07): an assistant makes the robot say a short line in the WIM-Z voice
# ---------------------------------------------------------------------------

SPEAK_MAX_CHARS = 280
SPEAK_PER_MINUTE = 6
SPEAK_ACTORS = {"connector", "agent"}
_speak_log: dict[str, list[float]] = {}   # actor label -> monotonic times of accepted speaks (last 60 s)


async def speak(dog_id: str, text: str, actor: Actor) -> dict:
    """Guard, synthesize with the WIM-Z ElevenLabs voice (the only voice; callers can't pick one),
    and send play_sound{url, kind: "tts"} to one of the owner's robots.

    Refusals: 403 speak_not_allowed (caller kind), 422 speak_text_invalid (empty / > 280 chars),
    403 speak_quiet_hours, 429 speak_rate (6 per minute per assistant)."""
    from app.voice_agent import tts

    if actor.kind not in SPEAK_ACTORS:
        raise HTTPException(status_code=403, detail={"code": "speak_not_allowed", "actor": actor.kind})
    text = " ".join((text or "").split())
    if not text or len(text) > SPEAK_MAX_CHARS:
        raise HTTPException(status_code=422, detail={"code": "speak_text_invalid", "max_chars": SPEAK_MAX_CHARS,
                                                     "chars": len(text)})
    if settings_service.in_quiet_hours(dog_id):
        raise HTTPException(status_code=403, detail={"code": "guard_denied", "guard": "speak_quiet_hours",
                                                     "reason": "the robot does not speak for assistants during quiet hours"})
    now = time.monotonic()
    recent = [t for t in _speak_log.get(actor.label, []) if now - t < 60.0]
    if len(recent) >= SPEAK_PER_MINUTE:
        raise HTTPException(status_code=429, detail={"code": "speak_rate", "per_minute": SPEAK_PER_MINUTE,
                                                     "retry_after_s": int(60 - (now - recent[0])) + 1})
    try:
        clip_id, url = await tts.synthesize(text)
    except tts.TTSError as e:
        raise HTTPException(status_code=503, detail={"code": "speak_tts_failed", "reason": str(e)[:160]})
    relay = Actor(kind="relay", id=f"speak:{actor.label}", user_id=actor.user_id)
    res = await dispatch(dog_id, "play_tts", {"url": url, "clip_id": clip_id}, relay, allow_relay=True)
    recent.append(now)
    _speak_log[actor.label] = recent
    logger.info(f"[SPEAK] {actor.label} -> {dog_id}: {len(text)} chars, clip {clip_id}, delivered={res.get('delivered')}")
    return {"ok": True, "command_id": res["command_id"], "device_id": res["device_id"], "clip_id": clip_id,
            "chars": len(text), "delivered": res["delivered"], "simulated": res["simulated"],
            "speaks_left_this_minute": SPEAK_PER_MINUTE - len(recent)}
