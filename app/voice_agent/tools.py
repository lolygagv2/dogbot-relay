"""Claude tool definitions + dispatcher for the voice agent.

Same twelve capabilities as the MCP server (re-implemented against the relay with actor
`agent`) plus capture_photo, run_trick and standby. Every call is audited in mcp_audit
with actor `agent`. Results are small dicts; refusals come back as
{ok: false, refused: true, reason} so the model can voice them.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import time
from typing import Any, Callable, Optional

from app.database import get_dog_by_id, get_user_dog_role, get_user_dogs
from app.mcp_server import oauth_db
from app.services.relay_client import RelayClient, RelayError, RelayRefusal
from app.services.robot_control import MOVE_ACTIONS as MOVE_SPEC, MOVE_WAIT_S, move_tool_result
from app.services.robot_control import LISTEN_EXTRA_WAIT_S, LISTEN_MAX_S, listen_tool_result
from app.services.robot_control import coach_mode_command, coach_mode_tool_result, sg_mode_command, sg_mode_tool_result
from app.services import schedule_tools, settings_tools

logger = logging.getLogger(__name__)

PHYSICAL_TOOLS = {"give_treat", "go_to", "move", "take_photo", "listen", "run_trick", "standby", "speak"}
GO_TO_TARGETS = ["dog", "dock"]
MOVE_ACTIONS = list(MOVE_SPEC)
TRICKS = ["sit", "down", "stay", "spin", "place", "paw", "speak"]
_MEDIA_KEYS = {"image_url", "clip_url", "photo", "image", "audio", "audio_url", "url", "video_url"}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _str(desc: str, enum: Optional[list[str]] = None) -> dict:
    d: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


# The Anthropic API rejects a request with more than 20 strict tools (400 "Too many strict
# tools"). Keep strict on tools with tricky or physical inputs; leave it off trivial ones.
MAX_STRICT_TOOLS = 20

TOOLS: list[dict] = [
    {"name": "get_dogs", "description": "List the owner's dogs (dog_id, name, breed).",
     "input_schema": _schema({}, [])},
    {"name": "get_day_summary",
     "description": "Daily roll-up for a dog: counts per event type, totals (bark seconds, treats vs cap, alone time), time-to-settle stats, top bark episodes. date: YYYY-MM-DD, today or yesterday.",
     "input_schema": _schema({"dog_id": _str("dog id"), "date": _str("YYYY-MM-DD | today | yesterday")}, ["dog_id", "date"]),
     "strict": True},
    {"name": "get_events",
     "description": "Recent events for a dog, newest first. type is one of bark_episode, intervention, settle, pacing, departure, arrival, treat, music, mission_run, coach_session, trick_attempt, status or empty for all.",
     "input_schema": _schema({"dog_id": _str("dog id"), "type": _str("event type or empty string"),
                              "limit": {"type": "integer", "description": "1-50"}}, ["dog_id", "type", "limit"]),
     "strict": True},
    {"name": "get_robot_status",
     "description": "Robot status: online, mode, battery, treats today vs daily cap, quiet hours.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "get_missions", "description": "Configured missions for a dog and their last run.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"])},
    {"name": "set_quiet_hours",
     "description": "Set quiet hours (HH:MM 24h local). The agent may only widen the window; narrowing is refused.",
     "input_schema": _schema({"dog_id": _str("dog id"), "start": _str("HH:MM"), "end": _str("HH:MM")},
                             ["dog_id", "start", "end"]), "strict": True},
    {"name": "set_music",
     "description": "Enable/disable calming music and set its volume (0-100). Pass volume -1 to keep the current volume.",
     "input_schema": _schema({"dog_id": _str("dog id"), "enabled": {"type": "boolean"},
                              "volume": {"type": "integer", "description": "0-100, or -1 to keep"}},
                             ["dog_id", "enabled", "volume"]), "strict": True},
    {"name": "set_sg_sensitivity",
     "description": "Set how sensitive Silent Guardian (bark intervention) is: level low, medium or high; min_episode_s = seconds of barking before it counts as an episode (1-600); escalate_after_s = seconds before it escalates (5-3600). Pass -1 for a number to keep its current value.",
     "input_schema": _schema({"dog_id": _str("dog id"), "level": _str("low | medium | high", ["low", "medium", "high"]),
                              "min_episode_s": {"type": "integer", "description": "1-600, or -1 to keep"},
                              "escalate_after_s": {"type": "integer", "description": "5-3600, or -1 to keep"}},
                             ["dog_id", "level", "min_episode_s", "escalate_after_s"]), "strict": True},
    {"name": "set_silent_guardian",
     "description": "Turn Silent Guardian (bark watch and intervention) on or off right now. on=true starts Silent Guardian mode; on=false puts the robot in standby. To run it at a set time use schedule_mission with type silent_guardian. The robot does not confirm the change, so say it was sent.",
     "input_schema": _schema({"dog_id": _str("dog id"), "on": {"type": "boolean", "description": "true = on, false = off"}},
                             ["dog_id", "on"]), "strict": True},
    # Not strict (the API caps strict tools at 20): optional fields may simply be omitted.
    {"name": "set_coach",
     "description": "Turn Coach mode (active trick training) on or off right now. on=true starts Coach mode; on=false puts the robot in standby. To run it at a set time use schedule_mission with type coach. The robot does not confirm the change, so say it was sent.",
     "input_schema": {"type": "object", "properties": {
         "dog_id": _str("dog id"), "on": {"type": "boolean", "description": "true = on, false = off (standby)"}},
         "required": ["dog_id", "on"]}},
    {"name": "get_trick_history",
     "description": "The dog's trick record from Coach cues: per trick attempts, successes, success_pct, avg latency and last attempt, plus recent attempts and totals. days 1-730 (default 30).",
     "input_schema": {"type": "object", "properties": {
         "dog_id": _str("dog id"), "days": {"type": "integer", "description": "1-730, default 30"}},
         "required": ["dog_id"]}},
    {"name": "set_treat_schedule",
     "description": "Change treat windows and limits; omitted fields keep their value. windows: list of {start, end, multiplier}; start/end HH:MM 24h local (may wrap midnight, no overlaps, at most 12); multiplier 0-3 scales treat frequency inside the window. Empty list clears them. default_multiplier (0-3) applies outside windows. daily_cap (0-100) = most treats per day; daily_cap_enabled false = no daily cap (unlimited), true = back on; max_per_hour (0-60, 0 = no hourly limit). The limits apply to all the owner's dogs; windows are per dog. Only change a limit when the owner says the number. Raising a limit alerts the owner. Say back what was saved.",
     "input_schema": {"type": "object", "properties": {
         "dog_id": _str("dog id"),
         "windows": {"type": "array", "items": {"type": "object", "properties": {
             "start": _str("HH:MM"), "end": _str("HH:MM"), "multiplier": {"type": "number", "description": "0-3"}},
             "required": ["start", "end", "multiplier"]}},
         "default_multiplier": {"type": "number", "description": "0-3; omit to keep"},
         "daily_cap": {"type": "integer", "description": "0-100 treats per day; omit to keep"},
         "daily_cap_enabled": {"type": "boolean", "description": "false = unlimited (no daily cap); omit to keep"},
         "max_per_hour": {"type": "integer", "description": "0-60 treats per hour, 0 = no hourly limit; omit to keep"}},
         "required": ["dog_id"]}},
    {"name": "set_notify_rule",
     "description": "Add a phone notification rule. event_type: bark_episode, intervention, settle, pacing, departure, arrival, treat, music or mission_run. Optional: min_duration_s; severity (bark_episode only) low|med|high meaning at least that severe; attach none|still|clip; cooldown_s between pushes. At most 10 rules per dog; edits and removals are done in the app.",
     "input_schema": {"type": "object", "properties": {
         "dog_id": _str("dog id"),
         "event_type": _str("event type", settings_tools.NOTIFY_EVENT_TYPES),
         "min_duration_s": {"type": "number", "description": "seconds; omit for any length"},
         "severity": _str("bark severity at least", settings_tools.SEVERITIES),
         "attach": _str("what to attach", settings_tools.ATTACH),
         "cooldown_s": {"type": "integer", "description": "0-86400 seconds"}},
         "required": ["dog_id", "event_type"]}},
    {"name": "get_schedule",
     "description": "List scheduled missions and ask the robot what it has loaded: each has id, name, type, time (local HH:MM), days, duration_min and, when the robot confirms, next_fire_at. confirmed_by_robot false means it is the relay's copy only.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "schedule_mission",
     "description": "Schedule a mission now (no approval step). type: a training mission (1-60 min) or silent_guardian / coach (the robot enters that mode at the time for duration_min, 5-720, then goes idle). time HH:MM 24h local. Only say it is on the robot when confirmed_by_robot is true; say the next run in words (e.g. 'tomorrow at 6 pm'), never the raw timestamp.",
     "input_schema": _schema({"dog_id": _str("dog id"), "type": _str("mission type", schedule_tools.ALL_TYPES),
                              "time": _str("HH:MM, 24h local time"),
                              "days": {"type": "array", "items": {"type": "string", "enum": schedule_tools.DAYS},
                                       "description": "days to run; empty array = every day"},
                              "duration_min": {"type": "integer", "description": "minutes; -1 for the default (2 for training, 120 for a mode)"},
                              "name": _str("short name, or empty string")},
                             ["dog_id", "type", "time", "days", "duration_min", "name"]), "strict": True},
    {"name": "cancel_mission",
     "description": "Delete a scheduled mission by mission_id (from get_schedule). The robot is asked to confirm it is gone.",
     "input_schema": _schema({"dog_id": _str("dog id"), "mission_id": _str("mission id")}, ["dog_id", "mission_id"]),
     "strict": True},
    {"name": "give_treat", "description": "Dispense one treat now (subject to the owner's daily cap).",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "go_to", "description": "Send the robot to the dog or back to the dock; it finishes on its own.",
     "input_schema": _schema({"dog_id": _str("dog id"), "target": _str("target", GO_TO_TARGETS)}, ["dog_id", "target"]),
     "strict": True},
    {"name": "move", "description": "One small supervised step at creep speed; take a photo, decide one step, repeat.",
     "input_schema": _schema({"dog_id": _str("dog id"), "action": _str("action", MOVE_ACTIONS),
                              "amount": {"type": "integer",
                                         "description": "forward/backward: 5-30 cm (use 15 if unsure); turn_left/turn_right: 15-90 degrees (use 30 if unsure)"}},
                             ["dog_id", "action", "amount"]),
     "strict": True},
    {"name": "take_photo", "description": "Take one new snapshot with the robot camera (no HUD) and look at it. A copy is kept so the owner's app can add it to its photo gallery; don't say it is on a timeline. Describe what you see. Never live video.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "listen", "description": "Asks the robot to listen for up to 15 seconds and returns what it heard, with the recording.",
     "input_schema": _schema({"dog_id": _str("dog id"),
                              "seconds": {"type": "integer", "description": "how long to listen, 1-15 seconds"}},
                             ["dog_id", "seconds"]),
     "strict": True},
    {"name": "get_event_photo", "description": "Look at the snapshot WIM-Z already took for an event (use an event_id from get_events where has_photo is true).",
     "input_schema": _schema({"dog_id": _str("dog id"), "event_id": _str("event id")}, ["dog_id", "event_id"]), "strict": True},
    {"name": "run_trick", "description": "Ask the robot to cue a trick.",
     "input_schema": _schema({"dog_id": _str("dog id"), "trick": _str("trick", TRICKS)}, ["dog_id", "trick"]),
     "strict": True},
    {"name": "speak", "description": "Make the robot say a short line out loud through its speaker, in the WIM-Z voice (at most 280 characters). Refused during quiet hours; at most 6 per minute.",
     "input_schema": _schema({"dog_id": _str("dog id"), "text": _str("what the robot should say, <= 280 characters")},
                             ["dog_id", "text"]), "strict": True},
    {"name": "standby", "description": "Put the robot into standby (idle mode).",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"])},
]


def _scalar(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _MEDIA_KEYS and (isinstance(v, (str, int, float, bool)) or v is None)}


class ToolContext:
    def __init__(self, user_id: str, source: str, role: Optional[str] = None, allow_physical: bool = True):
        self.user_id = user_id
        self.source = source           # robot | phone | event
        self.role = role
        self.allow_physical = allow_physical
        self.client = RelayClient(actor="agent", user_id=user_id)
        self.calls: list[dict] = []


def _check_dog(ctx: ToolContext, dog_id: str) -> Optional[dict]:
    if not get_dog_by_id(dog_id):
        return {"ok": False, "refused": True, "reason": "unknown_dog", "hint": "call get_dogs"}
    if not get_user_dog_role(ctx.user_id, dog_id):
        return {"ok": False, "refused": True, "reason": "dog_not_linked_to_account"}
    return None


async def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """Run one tool; never raises. Audited."""
    t0 = time.monotonic()
    outcome, status = "ok", None
    try:
        if name in PHYSICAL_TOOLS and not ctx.allow_physical:
            result = {"ok": False, "refused": True, "reason": "physical_actions_not_allowed_for_this_caller"}
        else:
            result = await _run(ctx, name, args)
        if result.get("refused"):
            outcome = "refused"
            status = result.pop("_status", None)
        return result
    except RelayError as e:
        outcome = "error"
        return {"ok": False, "reason": "relay_unavailable", "detail": str(e)[:120]}
    except Exception as e:  # pragma: no cover
        outcome = "error"
        logger.exception(f"[AGENT] tool {name} failed")
        return {"ok": False, "reason": "tool_error", "detail": f"{type(e).__name__}"}
    finally:
        dur = int((time.monotonic() - t0) * 1000)
        ctx.calls.append({"tool": name, "outcome": outcome})
        try:
            await asyncio.get_running_loop().run_in_executor(None, functools.partial(
                oauth_db.insert_audit, "agent", name, outcome, client_id=None, client_name=f"voice:{ctx.source}",
                user_id=ctx.user_id, dog_id=args.get("dog_id"), scopes=None, input_obj=args,
                relay_status=status, result_summary=None, duration_ms=dur))
        except Exception:  # pragma: no cover
            pass


async def _run(ctx: ToolContext, name: str, a: dict) -> dict:
    c = ctx.client
    if name == "get_dogs":
        return {"dogs": [{"dog_id": d.get("id") or d.get("dog_id"), "name": d.get("name"), "breed": d.get("breed")}
                         for d in get_user_dogs(ctx.user_id)]}
    dog_id = str(a.get("dog_id") or "")
    bad = _check_dog(ctx, dog_id)
    if bad:
        return bad
    try:
        if name == "get_day_summary":
            s = await c.get_summary(dog_id, str(a.get("date") or "today"))
            for ep in s.get("top_episodes", []):
                ep.pop("image_url", None)
            s.pop("computed_at", None)
            return s
        if name == "get_events":
            t = str(a.get("type") or "")
            limit = max(1, min(int(a.get("limit") or 10), 50))
            res = await c.list_events(dog_id, types=[t] if t else None, limit=limit)
            return {"events": [{"event_id": e["event_id"], "type": e["type"], "ts": e["ts"], "duration_s": e.get("duration_s"),
                                "has_photo": bool((e.get("data") or {}).get("image_url")),
                                "data": _scalar(e.get("data") or {})} for e in res.get("events", [])]}
        if name == "get_robot_status":
            st = await c.get_status(dog_id)
            last = st.get("last_status") or {}
            return {"online": st.get("robot_online"), "mode": st.get("mode"), "battery_pct": st.get("battery"),
                    "docked": last.get("docked"), "light_level": last.get("light_level"),
                    "treats_remaining_in_hopper": last.get("treats_remaining"), "treats_today": st.get("treats_today"),
                    "treat_daily_cap": st.get("treat_cap"), "quiet_hours": st.get("quiet_hours")}
        if name == "get_missions":
            setting = await c.get_setting(dog_id, "missions")
            return {"missions": [{k: m.get(k) for k in ("id", "type", "name", "enabled", "trigger")}
                                 for m in (setting.get("value") or {}).get("missions", [])]}
        if name == "set_quiet_hours":
            row = await c.put_setting(dog_id, "quiet_hours", {"enabled": True, "start": a["start"], "end": a["end"]})
            return {"ok": True, "quiet_hours": row["value"]}
        if name == "set_music":
            cur = (await c.get_setting(dog_id, "music_plan")).get("value") or {}
            cur["enabled"] = bool(a.get("enabled"))
            vol = int(a.get("volume", -1))
            if vol >= 0:
                cur["volume"] = min(100, vol)
            row = await c.put_setting(dog_id, "music_plan", cur)
            return {"ok": True, "music_enabled": row["value"]["enabled"], "volume": row["value"]["volume"]}
        if name == "set_sg_sensitivity":
            cur = (await c.get_setting(dog_id, "sg_sensitivity")).get("value") or {}
            cur["level"] = a["level"]
            for k in ("min_episode_s", "escalate_after_s"):
                v = int(a.get(k, -1))
                if v >= 0:
                    cur[k] = v
            row = await c.put_setting(dog_id, "sg_sensitivity", cur)
            v = row["value"]
            return {"ok": True, "level": v["level"], "min_episode_s": v["min_episode_s"],
                    "escalate_after_s": v["escalate_after_s"]}
        if name == "set_silent_guardian":
            on = bool(a.get("on"))
            res = await c.command(dog_id, "set_mode", sg_mode_command(on))
            return sg_mode_tool_result(on, res)
        if name == "set_coach":
            on = bool(a.get("on"))
            res = await c.command(dog_id, "set_mode", coach_mode_command(on))
            return coach_mode_tool_result(on, res)
        if name == "get_trick_history":
            h = await c.trick_history(dog_id, max(1, min(int(a.get("days") or 30), 730)), 5)
            for t in h.get("tricks", []):
                t.pop("success_rate", None)            # speak percents: success_pct
            h.get("totals", {}).pop("success_rate", None)
            return h
        if name == "set_treat_schedule":
            return await settings_tools.set_treat_schedule(c, dog_id, a.get("windows"), a.get("default_multiplier"),
                                                           a.get("daily_cap"), a.get("max_per_hour"),
                                                           a.get("daily_cap_enabled"))
        if name == "set_notify_rule":
            return await settings_tools.set_notify_rule(c, dog_id, str(a.get("event_type") or ""), a.get("min_duration_s"),
                                                        a.get("severity"), a.get("attach"), a.get("cooldown_s"))
        if name == "get_schedule":
            return await schedule_tools.get_schedule(c, dog_id)
        if name == "schedule_mission":
            return await schedule_tools.schedule_mission(c, dog_id, a["type"], a["time"], a.get("days") or [],
                                                         a.get("duration_min"), a.get("name") or None)
        if name == "cancel_mission":
            return await schedule_tools.cancel_mission(c, dog_id, str(a.get("mission_id") or ""))
        if name == "give_treat":
            res = await c.command(dog_id, "dispense_treat", {})
            g = res.get("guard") or {}
            return {"ok": True, "treats_today": g.get("treats_used"), "treat_daily_cap": g.get("treat_cap"),
                    "simulated": res.get("simulated")}
        if name == "speak":
            res = await c.speak(dog_id, str(a.get("text") or ""))
            return {"ok": True, "chars": res.get("chars"), "delivered": res.get("delivered"),
                    "simulated": res.get("simulated")}
        if name == "go_to":
            res = await c.command(dog_id, "go_to", {"target": a["target"]})
            return {"ok": True, "target": a["target"], "simulated": res.get("simulated")}
        if name == "listen":
            secs = int(a["seconds"])
            if not 1 <= secs <= LISTEN_MAX_S:
                return {"ok": False, "refused": True, "reason": "listen_seconds_out_of_range", "max": LISTEN_MAX_S}
            res = await c.command(dog_id, "listen", {"seconds": secs}, wait_s=secs + LISTEN_EXTRA_WAIT_S)
            fields, clip = listen_tool_result(res)
            if clip:
                # The Messages API takes no audio input, so the voice agent gets the labels and level only.
                fields["recording"] = "kept 1 hour for connectors; not playable here"
            return fields
        if name == "move":
            res = await c.command(dog_id, "move", {"action": a["action"], "amount": a["amount"]}, wait_s=MOVE_WAIT_S)
            return move_tool_result(a["action"], res)
        if name == "take_photo":
            res = await c.command(dog_id, "take_photo", {"with_hud": False}, wait_s=10)
            r = res.get("result") or {}
            if res.get("simulated"):
                return {"ok": False, "reason": "robot_offline_demo"}
            if res.get("timed_out"):
                return {"ok": False, "reason": "photo_timed_out", "photo_request_id": res.get("command_id")}
            inner = r.get("result") if isinstance(r.get("result"), dict) else {}
            image_url = inner.get("image_url") or inner.get("url")
            out = {"ok": bool(r.get("ok", True)), "saved": bool(r.get("ok", True)), "error": r.get("error"),
                   "photo_request_id": res.get("command_id")}
            if image_url and out["ok"]:
                try:
                    raw, ctype = await c.still(dog_id, image_url)
                    out["_image_b64"], out["_media_type"] = base64.b64encode(raw).decode(), ctype.split(";")[0].strip()
                except (RelayRefusal, RelayError) as e:
                    out["image_error"] = str(e)[:80]
            return out
        if name == "get_event_photo":
            raw, ctype = await c.event_photo(dog_id, str(a.get("event_id") or ""))
            return {"ok": True, "event_id": a.get("event_id"), "_image_b64": base64.b64encode(raw).decode(),
                    "_media_type": ctype.split(";")[0].strip()}
        if name == "run_trick":
            res = await c.command(dog_id, "run_trick", {"trick": a["trick"]}, wait_s=60)
            r = res.get("result") or {}
            if res.get("timed_out"):
                return {"ok": False, "reason": "trick_timed_out", "trick": a["trick"]}
            inner = r.get("result") if isinstance(r.get("result"), dict) else {}
            return {"ok": bool(r.get("ok", True)), "trick": a["trick"], "success": inner.get("success", r.get("ok", True)),
                    "error": r.get("error"), "simulated": res.get("simulated")}
        if name == "standby":
            res = await c.command(dog_id, "standby", {})
            return {"ok": True, "mode": "idle", "simulated": res.get("simulated")}
    except RelayRefusal as e:
        out = e.as_result(_status=e.status)
        if e.code == "treat_cap_reached":
            out["treats_today"], out["treat_daily_cap"] = e.detail.get("used"), e.detail.get("cap")
        return out
    return {"ok": False, "reason": "unknown_tool"}
