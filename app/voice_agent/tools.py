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

logger = logging.getLogger(__name__)

PHYSICAL_TOOLS = {"give_treat", "go_to", "take_photo", "run_trick", "standby", "speak"}
MISSION_TYPES = ["recall", "sit_stay", "down_stay", "place", "quiet", "find_treat", "sit", "down", "stay", "spin"]
GO_TO_TARGETS = ["dock", "bed", "kitchen", "door", "crate", "living_room", "owner"]
TRICKS = ["sit", "down", "stay", "spin", "place", "paw", "speak"]
_MEDIA_KEYS = {"image_url", "clip_url", "photo", "image", "audio", "audio_url", "url", "video_url"}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _str(desc: str, enum: Optional[list[str]] = None) -> dict:
    d: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


TOOLS: list[dict] = [
    {"name": "get_dogs", "description": "List the owner's dogs (dog_id, name, breed).",
     "input_schema": _schema({}, []), "strict": True},
    {"name": "get_day_summary",
     "description": "Daily roll-up for a dog: counts per event type, totals (bark seconds, treats vs cap, alone time), time-to-settle stats, top bark episodes. date: YYYY-MM-DD, today or yesterday.",
     "input_schema": _schema({"dog_id": _str("dog id"), "date": _str("YYYY-MM-DD | today | yesterday")}, ["dog_id", "date"]),
     "strict": True},
    {"name": "get_events",
     "description": "Recent events for a dog, newest first. type is one of bark_episode, intervention, settle, pacing, departure, arrival, treat, music, mission_run, status or empty for all.",
     "input_schema": _schema({"dog_id": _str("dog id"), "type": _str("event type or empty string"),
                              "limit": {"type": "integer", "description": "1-50"}}, ["dog_id", "type", "limit"]),
     "strict": True},
    {"name": "get_robot_status",
     "description": "Robot status: online, mode, battery, treats today vs daily cap, quiet hours.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "get_missions", "description": "Configured missions for a dog and their last run.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "set_quiet_hours",
     "description": "Set quiet hours (HH:MM 24h local). The agent may only widen the window; narrowing is refused.",
     "input_schema": _schema({"dog_id": _str("dog id"), "start": _str("HH:MM"), "end": _str("HH:MM")},
                             ["dog_id", "start", "end"]), "strict": True},
    {"name": "set_music",
     "description": "Enable/disable calming music and set its volume (0-100). Pass volume -1 to keep the current volume.",
     "input_schema": _schema({"dog_id": _str("dog id"), "enabled": {"type": "boolean"},
                              "volume": {"type": "integer", "description": "0-100, or -1 to keep"}},
                             ["dog_id", "enabled", "volume"]), "strict": True},
    {"name": "draft_mission",
     "description": "Propose a training mission as a draft the owner approves in the WIM-Z app (nothing runs until approved). type: recall, sit_stay, down_stay, place, quiet, find_treat, sit, down, stay, spin. trigger: schedule (needs time HH:MM), departure, or manual.",
     "input_schema": _schema({"dog_id": _str("dog id"), "type": _str("mission type", MISSION_TYPES),
                              "trigger": _str("schedule | departure | manual", ["schedule", "departure", "manual"]),
                              "time": _str("HH:MM for schedule, else empty string"),
                              "name": _str("short name"), "goal": _str("one-line goal")},
                             ["dog_id", "type", "trigger", "time", "name", "goal"]),
     "strict": True},
    {"name": "schedule_mission",
     "description": "Check/confirm a drafted mission by draft_id. Only drafts the owner approved in the app are scheduled; pending ones are refused with awaiting_owner_approval.",
     "input_schema": _schema({"dog_id": _str("dog id"), "draft_id": _str("draft id from draft_mission")}, ["dog_id", "draft_id"]),
     "strict": True},
    {"name": "give_treat", "description": "Dispense one treat now (subject to the owner's daily cap).",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "go_to", "description": "Send the robot to a named spot.",
     "input_schema": _schema({"dog_id": _str("dog id"), "target": _str("target", GO_TO_TARGETS)}, ["dog_id", "target"]),
     "strict": True},
    {"name": "take_photo", "description": "Take one new snapshot with the robot camera (no HUD) and look at it; it is also saved to the owner's timeline. Never live video.",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
    {"name": "get_event_photo", "description": "Look at the snapshot WIM-Z already took for an event (use an event_id from get_events where has_photo is true).",
     "input_schema": _schema({"dog_id": _str("dog id"), "event_id": _str("event id")}, ["dog_id", "event_id"]), "strict": True},
    {"name": "run_trick", "description": "Ask the robot to cue a trick.",
     "input_schema": _schema({"dog_id": _str("dog id"), "trick": _str("trick", TRICKS)}, ["dog_id", "trick"]),
     "strict": True},
    {"name": "speak", "description": "Make the robot say a short line out loud through its speaker, in the WIM-Z voice (at most 280 characters). Refused during quiet hours; at most 6 per minute.",
     "input_schema": _schema({"dog_id": _str("dog id"), "text": _str("what the robot should say, <= 280 characters")},
                             ["dog_id", "text"]), "strict": True},
    {"name": "standby", "description": "Put the robot into standby (idle mode).",
     "input_schema": _schema({"dog_id": _str("dog id")}, ["dog_id"]), "strict": True},
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
        if name == "draft_mission":
            trig = a.get("trigger") or "manual"
            tm = (a.get("time") or "").strip() or None
            if trig == "schedule" and not tm:
                return {"ok": False, "refused": True, "reason": "scheduled_mission_needs_time"}
            mid = f"m_{a['type']}_{int(time.time()) % 10000}"
            mission = {"id": mid, "type": a["type"], "name": (a.get("name") or a["type"])[:120], "enabled": True,
                       "trigger": {"kind": trig, "time": tm, "delay_s": 300, "cooldown_s": 7200},
                       "duration_s": 120, "reward_treats": 1, "goal": (a.get("goal") or "")[:240]}
            row = await c.create_draft(dog_id, mission, [], "proposed by voice")
            return {"ok": True, "draft_id": row["draft_id"], "status": "pending", "type": a["type"], "trigger": trig,
                    "time": tm, "message": "sent to the owner's app for approval"}
        if name == "schedule_mission":
            d = await c.get_draft(dog_id, str(a.get("draft_id") or ""))
            if d["status"] == "approved":
                return {"ok": True, "scheduled": True, "mission_id": d.get("mission_id")}
            if d["status"] == "pending":
                return {"ok": False, "refused": True, "reason": "awaiting_owner_approval"}
            return {"ok": False, "refused": True, "reason": f"draft_{d['status']}"}
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
