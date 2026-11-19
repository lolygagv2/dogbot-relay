"""Allowlisted robot control with guards (Dec 9 demo build, 2026-11-06).

Every HTTP actor (app, admin, MCP connector, voice agent) and the relay's own internal
callers (speak) go through `dispatch`. The outgoing robot message is built fresh
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
from app.services import robot_state, settings_service, sleep_wake

logger = logging.getLogger(__name__)

SAFE_MODES = {"idle", "silent_guardian", "coach", "mission"}  # never "manual" (drive) over HTTP
# ROBOT-STATE v2: set_mode / set_sleep (and standby = set_mode idle) wait for the robot's
# command_result (2000 ms); nothing is queued or retried. Sleep is not a mode: use set_sleep.
STATE_WAIT_S = 2.0
KNOWN_MISSION_TYPES = {"recall", "sit_stay", "find_treat", "place", "quiet", "down_stay", "spin", "sit", "down", "stay"}
KNOWN_TRICKS = {"sit", "down", "stay", "spin", "place", "paw", "speak"}
KNOWN_TARGETS = {"dog", "dock"}   # go_to: drive toward the detected dog, or return to dock
# move: one small supervised step at creep speed. action -> (unit, min, max, default)
MOVE_ACTIONS: dict[str, tuple[str, int, int, int]] = {
    "forward": ("cm", 5, 30, 15), "backward": ("cm", 5, 30, 15),
    "turn_left": ("deg", 15, 90, 30), "turn_right": ("deg", 15, 90, 30),
}

# What each actor kind may ask for. "relay" = the relay's own internal callers (not reachable
# over HTTP); since the departure triggers went (2026-11-18) only speak() uses it.
ACTOR_ALLOWED: dict[str, set[str]] = {
    "app":       {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "move", "take_photo", "standby", "run_trick", "listen", "schedule_sync", "smart_plug", "ultrasonic", "look", "set_sleep"},
    "admin":     {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "move", "take_photo", "standby", "run_trick", "listen", "schedule_sync", "smart_plug", "ultrasonic", "look", "set_sleep"},
    "agent":     {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "move", "take_photo", "standby", "run_trick", "play_tts", "listen", "schedule_sync", "smart_plug", "ultrasonic", "look", "set_sleep"},
    # PHOTO ACCESS (2026-11-06): connectors may take a still (camera scope, enforced by the MCP
    # server) — never with HUD, max one per 0.5 s per assistant. (Quiet hours no longer apply:
    # since 2026-11-07 they suppress only music, proactive speech and non-urgent pushes.)
    "connector": {"dispense_treat", "set_mode", "play_sound", "start_mission", "go_to", "move", "standby", "run_trick", "take_photo", "listen", "schedule_sync", "smart_plug", "ultrasonic", "look", "set_sleep"},
    # play_tts for "relay" = speak(): the URL is always a relay-made WIM-Z voice clip.
    "relay":     {"start_mission", "play_music", "play_tts"},
}

MOVE_WAIT_S = 8   # callers wait this long for the step's command_result
LISTEN_MAX_S = 15   # listen: record 1-15 s; the clip is kept 1 hour (media_files.AUDIO_TTL_S)
LISTEN_EXTRA_WAIT_S = 10   # callers wait seconds + this for the robot's command_result

# smart_plug (robot 248f9e5, 2026-11-11): the robot switches a WiFi plug on its own LAN (Adaptil
# diffuser). The plug choice lives on the robot only; the relay just validates and passes through.
SMART_PLUG_ACTIONS = {"status", "on", "off", "toggle", "scan", "configure", "remove"}
SMART_PLUG_ASSISTANT_ACTIONS = {"status", "on", "off"}   # connectors and the voice agent
SMART_PLUG_WAIT_S = 16   # the robot answers within 15 s (scan ~3 s, ~7 s when the plug is absent)


SELECT_DOG_DESCRIPTION = (
    "Choose which dog WIM-Z is with: the same choice as the dog picker in the app. WIM-Z names this "
    "dog in its greetings and recaps, and it is the dog a mode is for when no other dog is given. "
    "Starting or stopping a mode never changes it. The choice is stored at once and sent to every "
    "robot of the owner; selected_dog_applied is true once a robot has confirmed it. A robot that is "
    "offline gets this one stored choice when it reconnects; nothing else is queued.")


async def select_dog_for_owner(client, dog_id: str) -> dict:
    """The select_dog tool (MCP and voice agent): H2 on each of the dog's owner's robots."""
    from app.services.relay_client import RelayRefusal
    owner = get_dog_owner(dog_id)
    devices = get_user_paired_devices(owner) if owner else []
    if not devices:
        return {"ok": False, "refused": True, "reason": "no_paired_device"}
    robots = []
    for d in devices:
        try:
            robots.append(await client.select_dog(d, dog_id))
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
    out = {"ok": True, "selected_dog_id": dog_id,
           "robots": [{k: r.get(k) for k in ("device_id", "robot_online", "selected_dog_applied", "state_version")}
                      for r in robots]}
    online = [r for r in robots if r.get("robot_online")]
    out["message"] = ("Selected; the robot is confirming it." if online else
                      "Selected; the robot is offline and gets it when it reconnects.")
    return out


def move_tool_result(action: str, res: dict) -> dict:
    """Shape a dispatch() result for the MCP and voice-agent move tools."""
    if res.get("simulated"):
        return {"ok": False, "reason": "robot_offline_demo", "simulated": True}
    if res.get("timed_out"):
        return {"ok": False, "reason": "move_timed_out", "command_id": res.get("command_id")}
    r = res.get("result") or {}
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out = {"ok": r.get("success") is True, "action": action, "command_id": res.get("command_id")}
    for k in ("amount", "unit", "blocked", "reason"):
        if isinstance(inner.get(k), (str, int, float, bool)):
            out[k] = inner[k]
    if r.get("error"):
        out["error"] = str(r["error"])[:120]
    return out

def sg_mode_command(on: bool, dog_id: Optional[str] = None) -> dict:
    """set_silent_guardian: on -> silent_guardian (session dog = dog_id), off -> idle."""
    return {"mode": "silent_guardian", "dog_id": dog_id} if on else {"mode": "idle", "dog_id": None}


def coach_mode_command(on: bool, dog_id: Optional[str] = None) -> dict:
    """set_coach: on -> coach (session dog = dog_id), off -> idle (standby)."""
    return {"mode": "coach", "dog_id": dog_id} if on else {"mode": "idle", "dog_id": None}


def _robot_error(r: dict) -> str:
    err = r.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    return str(code or "refused")[:64]


def _mode_tool_result(key: str, label: str, on: bool, res: dict) -> dict:
    """Shape a set_mode dispatch() result (the robot confirmed it, or refused) for the mode tools."""
    mode = "silent_guardian" if key == "silent_guardian" and on else "coach" if on else "idle"
    out: dict[str, Any] = {"ok": True, key: "on" if on else "off", "mode": mode,
                           "delivered": bool(res.get("delivered")), "simulated": bool(res.get("simulated")),
                           "command_id": res.get("command_id")}
    if out["simulated"]:
        out["message"] = "Simulated (demo dog, no live robot)."
        return out
    r = res.get("result") or {}
    if not r.get("success"):
        out.update(ok=False, error=_robot_error(r), message=f"The robot did not change mode ({_robot_error(r)}).")
        return out
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out["mode"] = inner.get("mode", mode)
    out["session_dog_id"] = inner.get("session_dog_id")
    out["message"] = f"{label} is {'on' if on else 'off'}."
    return out


def sg_mode_tool_result(on: bool, res: dict) -> dict:
    return _mode_tool_result("silent_guardian", "Silent Guardian", on, res)


def coach_mode_tool_result(on: bool, res: dict) -> dict:
    return _mode_tool_result("coach", "Coach mode", on, res)


def sleep_tool_result(asleep: bool, res: dict) -> dict:
    """Shape a set_sleep dispatch() result for the set_sleep tools."""
    out: dict[str, Any] = {"ok": True, "asleep": asleep, "delivered": bool(res.get("delivered")),
                           "simulated": bool(res.get("simulated")), "command_id": res.get("command_id")}
    if out["simulated"]:
        out["message"] = "Simulated (demo dog, no live robot)."
        return out
    r = res.get("result") or {}
    if not r.get("success"):
        out.update(ok=False, error=_robot_error(r),
                   message=f"The robot did not {'go to sleep' if asleep else 'wake'} ({_robot_error(r)}).")
        return out
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out["asleep"] = inner.get("asleep", asleep)
    out["mode"] = inner.get("mode")
    out["message"] = "WIM-Z is asleep." if out["asleep"] else "WIM-Z is awake and ready."
    return out


def listen_tool_result(res: dict) -> tuple[dict, Optional[str]]:
    """Shape a dispatch() result for the listen tools: (fields, clip_url or None).
    fields: ok, labels (list of str), db (number), plus seconds/peak_db/clip_expires_in_s when given."""
    if res.get("simulated"):
        return {"ok": False, "reason": "robot_offline_demo", "simulated": True}, None
    if res.get("timed_out"):
        return {"ok": False, "reason": "listen_timed_out", "command_id": res.get("command_id")}, None
    r = res.get("result") or {}
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out: dict[str, Any] = {"ok": r.get("success") is True, "command_id": res.get("command_id")}
    labels = inner.get("labels")
    if isinstance(labels, list):
        # The robot sends [{"label": "Speech", "confidence": 0.97}, ...]; plain strings are accepted too.
        clean: list = []
        for x in labels[:20]:
            if isinstance(x, dict) and isinstance(x.get("label"), str):
                item: dict[str, Any] = {"label": x["label"][:64]}
                if isinstance(x.get("confidence"), (int, float)) and not isinstance(x.get("confidence"), bool):
                    item["confidence"] = round(float(x["confidence"]), 3)
                clean.append(item)
            elif isinstance(x, str):
                clean.append({"label": x[:64]})
        out["labels"] = clean
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    db = _num(inner.get("db"))
    if db is None:
        db = _num(inner.get("mean_db"))   # the robot's field name
    if db is not None:
        out["db"] = db
    for k in ("peak_db", "seconds"):
        if _num(inner.get(k)) is not None:
            out[k] = inner[k]
    if r.get("error"):
        out["error"] = str(r["error"])[:120]
    clip = next((inner[k] for k in ("clip_url", "audio_url", "url") if isinstance(inner.get(k), str) and inner[k]), None)
    if clip:
        from app.services.media_files import AUDIO_TTL_S
        out["clip_expires_in_s"] = AUDIO_TTL_S
    return out, clip


# ultrasonic (robot, 2026-11-12): the robot's tweeter, the same one Silent Guardian uses. App, admin and
# assistants (connectors: opt-in "ultrasonic" scope, only when the owner asks). The robot also enforces its
# enabled flag, a 20 s cooldown shared with Silent Guardian, and the caps below; the relay checks them
# first so bad requests never reach it.
ULTRASONIC_STYLES = {"before_quiet", "before_music", "nonstop_barking"}
ULTRASONIC_FREQS_HZ = {20000, 25000, 32000, 40000}
ULTRASONIC_MAX_LEVEL_PCT = 80
ULTRASONIC_MAX_S = 5.0
ULTRASONIC_WAIT_S = 10   # the burst is at most 5 s


def _num_in(raw: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not lo <= raw <= hi:
        return None
    return raw


def ultrasonic_payload(data: dict) -> dict:
    """Validate one ultrasonic request: {action: status} | {style} | a custom burst. Raises 422."""
    def bad(field: str, **extra):
        raise HTTPException(status_code=422, detail={"code": "bad_request", "field": field, **extra})

    if data.get("action") is not None:
        if data["action"] != "status":
            bad("action", allowed=["status"])
        return {"action": "status"}
    if data.get("style") is not None:
        if data["style"] not in ULTRASONIC_STYLES:
            bad("style", allowed=sorted(ULTRASONIC_STYLES))
        return {"style": data["style"]}
    out: dict[str, Any] = {}
    for field in ("freq_hz", "alt_freq_hz"):
        raw = data.get(field)
        if raw is None and field == "alt_freq_hz":
            continue
        if isinstance(raw, bool) or raw not in ULTRASONIC_FREQS_HZ:
            bad(field, value=raw, allowed=sorted(ULTRASONIC_FREQS_HZ))
        out[field] = int(raw)
    level = _num_in(data.get("level_pct"), 1, ULTRASONIC_MAX_LEVEL_PCT)
    if level is None:
        bad("level_pct", value=data.get("level_pct"), min=1, max=ULTRASONIC_MAX_LEVEL_PCT)
    out["level_pct"] = level
    dur = _num_in(data.get("duration_s"), 0.1, ULTRASONIC_MAX_S)
    if dur is None:
        bad("duration_s", value=data.get("duration_s"), min=0.1, max=ULTRASONIC_MAX_S)
    out["duration_s"] = dur
    for field in ("pulse_s", "gap_s"):
        if data.get(field) is None:
            continue
        v = _num_in(data[field], 0.05, ULTRASONIC_MAX_S)
        if v is None:
            bad(field, value=data[field], min=0.05, max=ULTRASONIC_MAX_S)
        out[field] = v
    return out


def ultrasonic_tool_result(res: dict) -> dict:
    """Shape a dispatch() result for the ultrasonic tools; the robot's reply fields pass through."""
    if res.get("simulated"):
        return {"ok": False, "reason": "robot_offline_demo", "simulated": True}
    if res.get("timed_out"):
        return {"ok": False, "reason": "ultrasonic_timed_out", "command_id": res.get("command_id")}
    r = res.get("result") or {}
    out: dict[str, Any] = {"ok": r.get("success") is True, "command_id": res.get("command_id")}
    if isinstance(r.get("result"), dict):
        out["result"] = r["result"]
    err = r.get("error")
    if err:
        out["error"] = err if isinstance(err, dict) else {"code": str(err)[:64]}
    return out


# look (robot d7f235b, 2026-11-12): one bounded camera pan/tilt step. Pan + is right, tilt + is up,
# 0,0 = calibrated centre; ~30 deg/s. Never touches video.
LOOK_ACTIONS = {"pan_left", "pan_right", "tilt_up", "tilt_down", "center"}
LOOK_MIN_DEG, LOOK_MAX_DEG, LOOK_DEFAULT_DEG = 5, 45, 15
LOOK_WAIT_S = 6   # the robot answers within 5 s


def look_tool_result(action: str, res: dict) -> dict:
    if res.get("simulated"):
        return {"ok": False, "reason": "robot_offline_demo", "simulated": True}
    if res.get("timed_out"):
        return {"ok": False, "reason": "look_timed_out", "command_id": res.get("command_id")}
    r = res.get("result") or {}
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out: dict[str, Any] = {"ok": r.get("success") is True, "action": action, "command_id": res.get("command_id")}
    for k in ("pan", "tilt", "limit_reached", "moved_degrees"):
        if isinstance(inner.get(k), (int, float, bool)):
            out[k] = inner[k]
    err = r.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    if code:
        out["error"] = str(code)[:64]
    return out


def smart_plug_tool_result(action: str, res: dict) -> dict:
    """Shape a dispatch() result for the set_smart_plug tools. plug_unreachable is not a fault
    (the plug may simply be unplugged): it is reported as available=false, never as an alert."""
    if res.get("simulated"):
        return {"ok": False, "reason": "robot_offline_demo", "simulated": True}
    if res.get("timed_out"):
        return {"ok": False, "reason": "smart_plug_timed_out", "command_id": res.get("command_id")}
    r = res.get("result") or {}
    inner = r.get("result") if isinstance(r.get("result"), dict) else {}
    out: dict[str, Any] = {"ok": r.get("success") is True, "action": action, "command_id": res.get("command_id")}
    for k in ("configured", "alias", "on", "reachable", "owner", "auto_off_in_s", "auto_off_at",
              "trigger_level", "auto_off_minutes", "last_change", "last_error"):
        if isinstance(inner.get(k), (str, int, float, bool)):
            out[k] = inner[k][:120] if isinstance(inner[k], str) else inner[k]
    err = r.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    if code:
        out["error"] = str(code)[:64]
    if code == "plug_unreachable" or inner.get("reachable") is False:
        out["available"] = False
        out["message"] = "Plug not available (it may be unplugged). This is not a fault."
    elif code == "no_plug" or inner.get("configured") is False:
        out["message"] = "No smart plug is set up on the robot; the owner sets one up in the app."
    return out


PHOTO_MIN_INTERVAL_S = 0.5   # connectors: snapshot-move-snapshot loops need fast stills
_last_photo: dict[str, float] = {}   # actor label -> monotonic time of last connector snapshot

# Belt and braces: these can never be the outgoing command name.
NEVER_EMIT = {
    "webrtc_request", "webrtc_offer", "webrtc_answer", "webrtc_ice", "webrtc_close", "webrtc_credentials",
    "camera_on", "camera_off", "start_stream", "stop_stream", "start_live_view", "video_start", "record_start",
    "download_song", "motor", "servo", "pan_tilt", "drive", "set_led", "led",
}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reason(data: dict) -> Optional[str]:
    """data.reason; a caller-supplied data.source (never trusted as the source) moves here."""
    r = data.get("reason")
    if r is None:
        r = data.get("source")
    return str(r)[:120] if r is not None else None


def build_robot_message(command: str, data: dict, actor: Actor, dog_id: str) -> tuple[dict, dict]:
    """Return (robot_message, guard_info). Raises HTTPException on denial."""
    allowed = ACTOR_ALLOWED.get(actor.kind, set())
    if command not in allowed:
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "command": command,
                                                     "actor": actor.kind})
    data = data if isinstance(data, dict) else {}
    command_id = str(uuid.uuid4())
    guard: dict[str, Any] = {}
    source = robot_state.source_for(actor.kind)

    match command:
        case "dispense_treat":
            guard = settings_service.check_treat_cap(dog_id, actor)
            command_id = guard["command_id"]
            out = ("dispense_treat", {"source": source, "command_id": command_id,
                                      "multiplier": guard["multiplier"], "dog_id": dog_id})
        case "standby":
            out = ("set_mode", {"mode": "idle", "dog_id": None, "source": source, "reason": "standby",
                                "timestamp": _iso()})
        case "schedule_sync":
            # The robot answers with command_result {result: {missions: [{id, enabled, next_fire_at}]}}
            out = ("schedule_sync", {"dog_id": dog_id, "source": source})
        case "set_sleep":
            if not isinstance(data.get("asleep"), bool):
                raise HTTPException(status_code=422, detail={"code": "bad_request", "field": "asleep"})
            out = ("set_sleep", {"asleep": data["asleep"], "source": source, "reason": _reason(data)})
        case "set_mode":
            mode = robot_state.valid_mode(data.get("mode"))
            if mode is None:
                raise HTTPException(status_code=422, detail={"code": robot_state.ERR_INVALID_MODE,
                                                             "mode": data.get("mode"), "allowed": list(robot_state.MODES)})
            if mode not in SAFE_MODES:
                raise HTTPException(status_code=403, detail={"code": "mode_not_allowed", "mode": mode,
                                                             "allowed": sorted(SAFE_MODES)})
            session_dog = data.get("dog_id") or None
            if session_dog is not None and get_dog_owner(str(session_dog)) != get_dog_owner(dog_id):
                raise HTTPException(status_code=422, detail={"code": "unknown_dog", "dog_id": session_dog})
            out = ("set_mode", {"mode": mode, "dog_id": session_dog, "source": source, "reason": _reason(data),
                                "timestamp": _iso()})
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
        case "move":
            action = str(data.get("action") or "")[:32]
            spec = MOVE_ACTIONS.get(action)
            if spec is None:
                raise HTTPException(status_code=422, detail={"code": "unknown_move_action", "action": action,
                                                             "allowed": sorted(MOVE_ACTIONS)})
            unit, lo, hi, default = spec
            raw = data.get("amount")
            try:
                amount = default if raw is None else int(raw)
            except (TypeError, ValueError):
                amount = None
            if amount is None or isinstance(raw, bool) or not lo <= amount <= hi:
                raise HTTPException(status_code=422, detail={"code": "move_amount_out_of_range", "action": action,
                                                             "amount": raw, "min": lo, "max": hi, "unit": unit})
            out = ("move", {"action": action, "amount": amount, "unit": unit, "speed": "creep",
                            "source": source, "command_id": command_id})
        case "listen":
            raw = data.get("seconds")
            try:
                seconds = int(raw)
            except (TypeError, ValueError):
                seconds = None
            if seconds is None or isinstance(raw, bool) or not 1 <= seconds <= LISTEN_MAX_S:
                raise HTTPException(status_code=422, detail={"code": "listen_seconds_out_of_range", "seconds": raw,
                                                             "min": 1, "max": LISTEN_MAX_S})
            out = ("listen", {"seconds": seconds, "source": source, "command_id": command_id})
        case "smart_plug":
            action = str(data.get("action") or "")[:16]
            if action not in SMART_PLUG_ACTIONS:
                raise HTTPException(status_code=422, detail={"code": "invalid_action", "action": action,
                                                             "allowed": sorted(SMART_PLUG_ACTIONS)})
            if actor.kind in ("connector", "agent") and action not in SMART_PLUG_ASSISTANT_ACTIONS:
                raise HTTPException(status_code=403, detail={"code": "smart_plug_action_not_allowed",
                                                             "action": action,
                                                             "allowed": sorted(SMART_PLUG_ASSISTANT_ACTIONS)})
            plug: dict[str, Any] = {"action": action}
            if action == "configure":
                dev = data.get("device_id")
                if not isinstance(dev, str) or not dev.strip():
                    raise HTTPException(status_code=422, detail={"code": "missing_device_id"})
                plug["device_id"] = dev.strip()[:128]
                if data.get("alias") is not None:
                    if not isinstance(data["alias"], str):
                        raise HTTPException(status_code=422, detail={"code": "invalid_value", "field": "alias"})
                    plug["alias"] = data["alias"].strip()[:64]
                for field, lo, hi in (("trigger_level", 1, 4), ("auto_off_minutes", 1, 1440)):
                    raw = data.get(field)
                    if raw is None:
                        continue   # omitted fields keep their value on the robot
                    if isinstance(raw, bool) or not isinstance(raw, int) or not lo <= raw <= hi:
                        raise HTTPException(status_code=422, detail={"code": "invalid_value", "field": field,
                                                                     "value": raw, "min": lo, "max": hi})
                    plug[field] = raw
            out = ("smart_plug", {**plug, "source": source})
        case "ultrasonic":
            out = ("ultrasonic", {**ultrasonic_payload(data), "dog_id": dog_id, "source": source})
        case "look":
            action = str(data.get("action") or "")[:16]
            if action not in LOOK_ACTIONS:
                raise HTTPException(status_code=422, detail={"code": "invalid_action", "action": action,
                                                             "allowed": sorted(LOOK_ACTIONS)})
            payload: dict[str, Any] = {"action": action}
            if action != "center":
                raw = data.get("degrees")
                deg = LOOK_DEFAULT_DEG if raw is None else raw
                if isinstance(deg, bool) or not isinstance(deg, (int, float)) or not LOOK_MIN_DEG <= deg <= LOOK_MAX_DEG:
                    raise HTTPException(status_code=422, detail={"code": "invalid_value", "degrees": raw,
                                                                 "min": LOOK_MIN_DEG, "max": LOOK_MAX_DEG})
                payload["degrees"] = deg
            out = ("look", {**payload, "source": source})
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
    # ROBOT-STATE v2: the relay stamps data.source on every command from who called; anything the
    # caller sent as source travels as data.reason instead.
    payload["source"] = source
    if "reason" not in payload and _reason(data) is not None:
        payload["reason"] = _reason(data)
    keep_null = {"dog_id", "reason"} if name in robot_state.STATE_COMMANDS else set()
    payload = {k: v for k, v in payload.items() if v is not None or k in keep_null}
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
        raise refusal(robot_state.ERR_OFFLINE)

    return await _deliver(message, guard, target, dog_id, actor, wait_s)


# Commands an actor may send to one robot without naming a dog (ROBOT-STATE v2 section 9: the voice
# agent speaks with no dog selected instead of borrowing the owner's oldest dog).
DEVICE_ONLY_COMMANDS = {"agent": {"play_tts"}}


async def dispatch_device(device_id: str, command: str, data: dict, actor: Actor) -> dict:
    """Guard, build and deliver one dog-less command to one robot (nothing is simulated or queued)."""
    from app.connection_manager import get_connection_manager
    if command not in DEVICE_ONLY_COMMANDS.get(actor.kind, set()):
        raise HTTPException(status_code=403, detail={"code": "command_not_allowed", "command": command,
                                                     "actor": actor.kind})
    message, guard = build_robot_message(command, data, actor, None)
    if not get_connection_manager().is_robot_online(device_id):
        raise refusal(robot_state.ERR_OFFLINE)
    return await _deliver(message, guard, device_id, None, actor, None)


async def _deliver(message: dict, guard: dict, target: str, dog_id: Optional[str], actor: Actor,
                   wait_s: Optional[float]) -> dict:
    from app.services import command_results
    from app.connection_manager import get_connection_manager
    message["device_id"] = target

    # Wake-first (ROBOT-STATE v2): EVERY command (but set_sleep) to a robot whose state says asleep
    # sends set_sleep false first and waits up to 6000 ms for it. Not confirmed -> robot_timeout (or
    # the robot's own error, e.g. expired) and the command is never sent. No retry.
    wake_info = None
    if sleep_wake.needs_wake(target, message["command"]):
        wake_info = await sleep_wake.ensure_awake(target, reason=message["command"], source=actor.kind)
        if not wake_info.get("ready"):
            raise refusal(wake_info.get("error") or "not_awake")
        sleep_wake.restamp(message)

    command_results.register(message["command_id"], dog_id=dog_id, device_id=target, command=message["command"],
                             source=message["data"].get("source"), reason=message["data"].get("reason"))
    delivered = await get_connection_manager().send_to_robot(target, message)
    if not delivered:
        raise refusal(robot_state.ERR_OFFLINE)
    logger.info(f"[CONTROL] {actor.label} -> {target}: {message['command']} ({message['command_id']})")
    out = {"command_id": message["command_id"], "device_id": target, "command_sent": message["command"],
           "delivered": True, "simulated": False, "guard": guard}
    if wake_info is not None:
        out["wake"] = wake_info
    # Every command is answered within 2000 ms (set_sleep false: 6000 ms); silence -> robot_timeout.
    # Commands whose result arrives when the work is done (listen, take_photo, move, run_trick,
    # smart_plug, ...) keep their caller's longer wait and report timed_out as before.
    budget = command_budget_s(message)
    long_wait = bool(wait_s) and float(wait_s) > budget
    res = await command_results.wait(message["command_id"], float(wait_s) if long_wait else budget)
    if res is None:
        if not long_wait:
            raise refusal(robot_state.ERR_TIMEOUT)
        out["timed_out"] = True
        out["result"] = None
        return out
    if res.get("success") is not True and res.get("error") == ERR_EXPIRED:
        raise refusal(ERR_EXPIRED)   # the robot could not begin it in time and dropped it
    out["result"] = {k: res.get(k) for k in ("success", "result", "error", "duration_ms", "timestamp")}
    return out


ERR_EXPIRED = "expired"
_REFUSAL_STATUS = {robot_state.ERR_OFFLINE: 409, robot_state.ERR_TIMEOUT: 504, ERR_EXPIRED: 409}


def refusal(code: str) -> HTTPException:
    """Section 5 refusals, code only, so connector tools return exactly {ok, refused, reason}."""
    return HTTPException(status_code=_REFUSAL_STATUS.get(code, 409), detail={"code": code})


def command_budget_s(message: dict) -> float:
    """How long the robot has to answer: set_sleep {asleep: false} 6000 ms, every other command 2000 ms."""
    if message.get("command") == "set_sleep" and (message.get("data") or {}).get("asleep") is False:
        return sleep_wake.WAKE_TIMEOUT_S
    return robot_state.COMMAND_TIMEOUT_S


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
