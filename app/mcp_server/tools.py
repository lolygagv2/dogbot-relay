"""MCP tools exposed to outside assistants (Dec 9 demo build, 2026-11-06).

Read tools: get_dogs, get_day_summary, get_events, get_trend, get_trick_history, get_missions, get_robot_status, get_schedule
Control tools: set_quiet_hours, set_music, set_sg_sensitivity, set_silent_guardian, set_coach, set_smart_plug, set_treat_schedule, set_notify_rule, schedule_mission, cancel_mission, give_treat, go_to, move, speak
Media tools: get_event_photo (photos), take_photo (camera), listen (listen)

Every tool: checks the token's scope, checks the user's role on the dog locally, then
calls the relay through RelayClient (actor `connector:<client_id>`). Relay refusals come
back as ordinary results `{ok: false, refused: true, reason, ...}` so the assistant can
tell the user why. Media is limited to stills (photos / camera scopes) and listen() clips (listen
scope, kept 1 hour); never video. Event payloads are whitelisted to scalar fields.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TypedDict
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Audio, Image
from mcp.types import ToolAnnotations

from app.database import get_dog_by_id, get_user_dog_role, get_user_dogs
from app.mcp_server.authz import audited, require_scope
from app.services.relay_client import RelayClient, RelayError, RelayRefusal
from app.services.robot_control import MOVE_ACTIONS as _MOVE_SPEC, MOVE_WAIT_S, move_tool_result as _move_result
from app.services.robot_control import LISTEN_EXTRA_WAIT_S, LISTEN_MAX_S, listen_tool_result
from app.services.robot_control import coach_mode_command, coach_mode_tool_result, sg_mode_command, sg_mode_tool_result
from app.services.robot_control import SMART_PLUG_ASSISTANT_ACTIONS, SMART_PLUG_WAIT_S, smart_plug_tool_result
from app.services import schedule_tools, settings_tools
from app.services.event_store import cue_rates as event_store_rates


class TreatWindowArg(TypedDict):
    start: str          # HH:MM 24h local
    end: str            # HH:MM 24h local
    multiplier: float   # 0-3

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

EVENT_TYPES = ["bark_episode", "intervention", "settle", "pacing", "departure", "arrival", "treat", "music",
               "mission_run", "status", "coach_session", "trick_attempt"]
GO_TO_TARGETS = ["dog", "dock"]
SG_LEVELS = ["low", "medium", "high"]
TREND_DAILY_MAX_DAYS = 90     # up to this many days: one row per day
TREND_MAX_DAYS = 730          # beyond 90: one row per week (Monday-Sunday), up to two years
TREND_METRICS = ["treats", "bark_episodes", "bark_s", "interventions", "pacing_s", "alone_s",
                 "missions_success", "missions_fail", "sg_s", "coach_s", "coach_sessions", "cues", "cue_successes"]
MOVE_ACTIONS = list(_MOVE_SPEC)
_MEDIA_KEYS = {"image_url", "clip_url", "photo", "image", "audio", "audio_url", "url", "video_url"}
_HM = re.compile(r"^\d{2}:\d{2}$")


def _client(tool: str) -> tuple[RelayClient, str]:
    token = require_scope(tool)
    try:
        return RelayClient(actor=f"connector:{token.client_id}", user_id=token.subject or ""), token.subject or ""
    except RelayError as e:
        raise ToolError(f"WIM-Z relay is not reachable: {e}")


def _check_dog(user_id: str, dog_id: str) -> dict:
    dog = get_dog_by_id(dog_id)
    if not dog:
        raise ToolError(f"Unknown dog_id {dog_id!r}. Call get_dogs first.")
    if not get_user_dog_role(user_id, dog_id):
        raise ToolError("This dog is not linked to your WIM-Z account.")
    return dog


def _scalar(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _MEDIA_KEYS and isinstance(v, (str, int, float, bool)) or v is None}


def _hm_ok(v: str) -> bool:
    if not isinstance(v, str) or not _HM.match(v):
        return False
    h, m = int(v[:2]), int(v[3:])
    return 0 <= h <= 23 and 0 <= m <= 59


def _minutes(start: str, end: str) -> set[int]:
    s, e = int(start[:2]) * 60 + int(start[3:]), int(end[:2]) * 60 + int(end[3:])
    if s == e:
        return set(range(1440))
    return set(range(s, e)) if s < e else set(range(s, 1440)) | set(range(0, e))


def register_tools(mcp: MCPServer) -> None:
    # ------------------------------------------------------------------ read

    @mcp.tool(name="get_dogs", annotations=READ)
    @audited("get_dogs")
    async def get_dogs() -> dict:
        """List the dogs linked to the signed-in WIM-Z account (dog_id, name, breed, role)."""
        token = require_scope("get_dogs")
        dogs = get_user_dogs(token.subject or "")
        out = []
        for d in dogs:
            out.append({
                "dog_id": d.get("id") or d.get("dog_id"),
                "name": d.get("name"),
                "breed": d.get("breed"),
                "role": d.get("role"),
            })
        return {"dogs": out}

    @mcp.tool(name="get_day_summary", annotations=READ)
    @audited("get_day_summary")
    async def get_day_summary(dog_id: str, date: str = "today") -> dict:
        """Daily roll-up for one dog: counts per event type, totals (bark seconds, treats vs cap,
        alone time, music, sg_s / coach_s, and coaching: cues = trick cues given, cue_successes,
        success_rate 0-1 and success_pct, cues_by_trick = per-trick {cues, successes}), time-to-settle
        stats and the top 3 bark episodes. coach_s is the day's Coach sessions when the robot
        reports them, otherwise time in Coach mode.
        date: YYYY-MM-DD, "today" or "yesterday" (in the dog's timezone)."""
        client, user_id = _client("get_day_summary")
        _check_dog(user_id, dog_id)
        try:
            summary = await client.get_summary(dog_id, date)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        for ep in summary.get("top_episodes", []):
            ep.pop("image_url", None)
        return summary

    @mcp.tool(name="get_events", annotations=READ)
    @audited("get_events")
    async def get_events(dog_id: str, since: Optional[str] = None, until: Optional[str] = None,
                         type: Optional[str] = None, limit: int = 25) -> dict:
        """Recent events for a dog, newest first. since/until are ISO-8601 timestamps; type is one of
        bark_episode, intervention, settle, pacing, departure, arrival, treat, music, mission_run, status
        (comma-separated allowed). limit 1-100."""
        client, user_id = _client("get_events")
        _check_dog(user_id, dog_id)
        limit = max(1, min(int(limit), 100))
        types = None
        if type:
            types = [t.strip() for t in type.split(",") if t.strip()]
            bad = [t for t in types if t not in EVENT_TYPES]
            if bad:
                raise ToolError(f"Unknown event type(s): {', '.join(bad)}")
        try:
            res = await client.list_events(dog_id, since=since, until=until, types=types, limit=limit)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        events = []
        for ev in res.get("events", []):
            data = ev.get("data") or {}
            events.append({"event_id": ev["event_id"], "type": ev["type"], "ts": ev["ts"],
                           "duration_s": ev.get("duration_s"), "source": ev.get("source"),
                           "has_photo": bool(data.get("image_url")),   # fetch with get_event_photo (photos scope)
                           "data": _scalar(data)})
        return {"dog_id": dog_id, "events": events, "count": len(events)}

    @mcp.tool(name="get_trend", annotations=READ)
    @audited("get_trend")
    async def get_trend(dog_id: str, days: int = 7, compare_dog_id: Optional[str] = None) -> dict:
        """Trend series over the last `days` days (1-730, default 7; today is in the last row):
        treats, bark episodes, bark seconds, interventions, pacing, alone time, missions, median
        time-to-settle, sg_s / coach_s (seconds the robot spent in Silent Guardian / Coach), and
        coaching: coach_sessions, cues, cue_successes and success_rate per row; totals add
        success_rate / success_pct over the whole period and cues_by_trick (per-trick cues, successes).
        Up to 90 days there is one row per day (granularity "day"). Over 90 days there is one row
        per week, Monday to Sunday (granularity "week", `date` = the week's Monday, `weeks` = row
        count); the last week is the current one and is partial, and a weekly settle_median_s is
        a median of the daily medians.
        Returns totals and the delta versus the previous period of the same length.
        compare_dog_id: a second dog on the same dates; adds `compare` (its series and totals) and
        `comparison` (per metric: dog, compare, difference = dog - compare, pct_difference vs compare).
        Robot mode time is shared by every dog in the home unless a mode session was for one dog,
        so sg_s / coach_s are often equal between dogs. Numbers only."""
        client, user_id = _client("get_trend")
        _check_dog(user_id, dog_id)
        if compare_dog_id is not None:
            if compare_dog_id == dog_id:
                raise ToolError("compare_dog_id must be a different dog.")
            _check_dog(user_id, compare_dog_id)
        days = max(1, min(int(days), TREND_MAX_DAYS))
        try:
            status = await client.get_status(dog_id)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        tz = ZoneInfo(status.get("timezone") or "UTC")
        today = datetime.now(timezone.utc).astimezone(tz).date()
        weekly = days > TREND_DAILY_MAX_DAYS
        if weekly:
            n = -(-days // 7)   # weeks, rounded up
            this_monday = today - timedelta(days=today.weekday())
            keys = [(this_monday - timedelta(weeks=i)).isoformat() for i in range(2 * n)]   # newest first
            fetch = client.get_week_summary
        else:
            n = days
            keys = [(today - timedelta(days=i)).isoformat() for i in range(2 * n)]          # newest first
            fetch = client.get_summary
        sem = asyncio.Semaphore(8)

        async def one(dog: str, d: str) -> dict:
            async with sem:
                try:
                    return await fetch(dog, d)
                except (RelayRefusal, RelayError):
                    return {"date": d, "counts": {}, "totals": {}, "time_to_settle": {}}

        def row(s: dict) -> dict:
            c, t, tts = s.get("counts", {}), s.get("totals", {}), s.get("time_to_settle", {})
            return {"date": s.get("date"), "treats": t.get("treats", 0), "bark_episodes": c.get("bark_episode", 0),
                    "bark_s": t.get("bark_s", 0), "interventions": c.get("intervention", 0),
                    "pacing_s": t.get("pacing_s", 0), "alone_s": t.get("alone_s", 0),
                    "missions_success": t.get("missions_success", 0), "missions_fail": t.get("missions_fail", 0),
                    "sg_s": t.get("sg_s", 0), "coach_s": t.get("coach_s", 0),
                    "coach_sessions": c.get("coach_session", 0), "cues": t.get("cues", 0),
                    "cue_successes": t.get("cue_successes", 0), "success_rate": t.get("success_rate"),
                    "settle_median_s": tts.get("median_s"), "_by_trick": t.get("cues_by_trick") or {}}

        def totals(rs: list[dict]) -> dict:
            out = {m: sum((r.get(m) or 0) for r in rs) for m in TREND_METRICS}
            med = [r["settle_median_s"] for r in rs if r.get("settle_median_s") is not None]
            out["settle_median_s_avg"] = round(sum(med) / len(med)) if med else None
            out.update(event_store_rates(out["cues"], out["cue_successes"]))
            by: dict[str, dict] = {}
            for r in rs:
                for trick, v in (r.get("_by_trick") or {}).items():
                    b = by.setdefault(trick, {"cues": 0, "successes": 0})
                    b["cues"] += v.get("cues", 0)
                    b["successes"] += v.get("successes", 0)
            out["cues_by_trick"] = {k: {**v, **event_store_rates(v["cues"], v["successes"])} for k, v in sorted(by.items())}
            return out

        def rate_delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
            return None if a is None or b is None else round(a - b, 2)

        jobs = [one(dog_id, d) for d in keys]
        if compare_dog_id:
            jobs += [one(compare_dog_id, d) for d in keys[:n]]
        summaries = await asyncio.gather(*jobs)
        rows = [row(s) for s in summaries[:2 * n]]
        for r, d in zip(rows, keys):
            r["date"] = d
        current, previous = list(reversed(rows[:n])), list(reversed(rows[n:]))
        cur_t, prev_t = totals(current), totals(previous)
        delta = {m: (cur_t[m] - prev_t[m]) for m in TREND_METRICS}
        delta["success_rate"] = rate_delta(cur_t["success_rate"], prev_t["success_rate"])
        for r in current + previous:
            r.pop("_by_trick", None)
        out = {"dog_id": dog_id, "days": days, "granularity": "week" if weekly else "day",
               "from": current[0]["date"], "to": today.isoformat() if weekly else current[-1]["date"],
               "series": current, "totals": cur_t, "previous_period_totals": prev_t, "delta": delta}
        if weekly:
            out["weeks"] = n
        if compare_dog_id:
            other = list(reversed([row(s) for s in summaries[2 * n:]]))
            for r, d in zip(other, reversed(keys[:n])):
                r["date"] = d   # same dates as `series`, even for a bucket the relay could not summarise
            other_t = totals(other)
            for r in other:
                r.pop("_by_trick", None)
            comparison = {}
            for m in [*TREND_METRICS, "settle_median_s_avg", "success_rate"]:
                a, b = cur_t[m], other_t[m]
                if a is None or b is None:
                    comparison[m] = {"dog": a, "compare": b, "difference": None, "pct_difference": None}
                else:
                    comparison[m] = {"dog": a, "compare": b, "difference": round(a - b, 2),
                                     "pct_difference": round((a - b) / b * 100) if b else None}
            out["compare"] = {"dog_id": compare_dog_id, "series": other, "totals": other_t}
            out["comparison"] = comparison
        return out

    @mcp.tool(name="get_missions", annotations=READ)
    @audited("get_missions")
    async def get_missions(dog_id: str) -> dict:
        """Configured missions for a dog (id, type, name, enabled, trigger) plus the most recent run of each."""
        client, user_id = _client("get_missions")
        _check_dog(user_id, dog_id)
        try:
            setting = await client.get_setting(dog_id, "missions")
            runs = await client.list_events(dog_id, types=["mission_run"], limit=50)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        last: dict[str, dict] = {}
        for ev in runs.get("events", []):
            mid = (ev.get("data") or {}).get("mission_id") or (ev.get("data") or {}).get("mission_type")
            if mid and mid not in last:
                last[mid] = {"ts": ev["ts"], "result": (ev.get("data") or {}).get("result")}
        missions = []
        for m in (setting.get("value") or {}).get("missions", []):
            missions.append({**m, "last_run": last.get(m["id"]) or last.get(m.get("type"))})
        return {"dog_id": dog_id, "missions": missions, "version": setting.get("version")}

    @mcp.tool(name="get_robot_status", annotations=READ)
    @audited("get_robot_status")
    async def get_robot_status(dog_id: str) -> dict:
        """Robot status for a dog: online, mode, battery, treats today vs daily cap, quiet hours, timezone."""
        client, user_id = _client("get_robot_status")
        _check_dog(user_id, dog_id)
        try:
            st = await client.get_status(dog_id)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        last = st.get("last_status") or {}
        return {"dog_id": dog_id, "online": st.get("robot_online"), "mode": st.get("mode"),
                "battery_pct": st.get("battery"), "docked": last.get("docked"), "light_level": last.get("light_level"),
                "treats_remaining_in_hopper": last.get("treats_remaining"),
                "treats_today": st.get("treats_today"), "treat_daily_cap": st.get("treat_cap"),
                "quiet_hours": st.get("quiet_hours"), "timezone": st.get("timezone"),
                "last_seen": st.get("last_status_ts"), "demo_replay_active": st.get("replay_active")}

    # --------------------------------------------------------------- control

    @mcp.tool(name="set_quiet_hours", annotations=CONTROL)
    @audited("set_quiet_hours")
    async def set_quiet_hours(dog_id: str, start: str, end: str) -> dict:
        """Turn on or widen the dog's quiet hours, as HH:MM 24h local times, e.g. start "22:00"
        end "07:00". Quiet hours are off unless the owner opts in. While on, they hold back music,
        the robot's own greeting / low-treat announcements and non-urgent notifications; bark
        alerts always get through. Assistants may turn quiet hours on or WIDEN the window;
        narrowing or turning them off must be done by the owner in the app."""
        client, user_id = _client("set_quiet_hours")
        _check_dog(user_id, dog_id)
        if not (_hm_ok(start) and _hm_ok(end)):
            raise ToolError("start and end must be HH:MM (24h).")
        try:
            current = (await client.get_setting(dog_id, "quiet_hours")).get("value") or {}
            if current.get("enabled", True) and not _minutes(current["start"], current["end"]) <= _minutes(start, end):
                return {"ok": False, "refused": True, "reason": "quiet_hours_can_only_widen",
                        "current": {"start": current["start"], "end": current["end"]},
                        "requested": {"start": start, "end": end},
                        "hint": "Ask the owner to narrow quiet hours in the WIM-Z app."}
            row = await client.put_setting(dog_id, "quiet_hours", {"enabled": True, "start": start, "end": end})
        except RelayRefusal as e:
            return e.as_result(_status=e.status, requested={"start": start, "end": end})
        return {"ok": True, "quiet_hours": row["value"], "previous": {"start": current.get("start"),
                "end": current.get("end")}, "version": row["version"]}

    @mcp.tool(name="set_music", annotations=CONTROL)
    @audited("set_music")
    async def set_music(dog_id: str, enabled: bool = True, volume: Optional[int] = None,
                        on_departure_track: Optional[str] = None, on_departure_delay_s: Optional[int] = None,
                        on_departure_duration_min: Optional[int] = None) -> dict:
        """Update the calming-music plan: enable/disable, volume 0-100, and what plays when the owner
        leaves (track name, delay in seconds, duration in minutes). Omitted fields keep their value."""
        client, user_id = _client("set_music")
        _check_dog(user_id, dog_id)
        try:
            cur = (await client.get_setting(dog_id, "music_plan")).get("value") or {}
            plan = dict(cur)
            plan["enabled"] = bool(enabled)
            if volume is not None:
                plan["volume"] = int(volume)
            od = dict(plan.get("on_departure") or {"track": "calm_01", "delay_s": 60, "duration_min": 30})
            if on_departure_track is not None:
                od["track"] = str(on_departure_track)[:64]
            if on_departure_delay_s is not None:
                od["delay_s"] = int(on_departure_delay_s)
            if on_departure_duration_min is not None:
                od["duration_min"] = int(on_departure_duration_min)
            plan["on_departure"] = od
            row = await client.put_setting(dog_id, "music_plan", plan)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "music_plan": row["value"], "version": row["version"]}

    @mcp.tool(name="set_sg_sensitivity", annotations=CONTROL)
    @audited("set_sg_sensitivity")
    async def set_sg_sensitivity(dog_id: str, level: Optional[str] = None, min_episode_s: Optional[int] = None,
                                 escalate_after_s: Optional[int] = None) -> dict:
        """Set how sensitive Silent Guardian (bark intervention) is: level low, medium or high; min_episode_s = seconds of barking before it counts as an episode (1-600); escalate_after_s = seconds before it escalates (5-3600). Omitted values keep their current setting."""
        client, user_id = _client("set_sg_sensitivity")
        _check_dog(user_id, dog_id)
        if level is not None and level not in SG_LEVELS:
            raise ToolError(f"level must be one of: {', '.join(SG_LEVELS)}")
        try:
            cur = (await client.get_setting(dog_id, "sg_sensitivity")).get("value") or {}
            new = dict(cur)
            if level is not None:
                new["level"] = level
            if min_episode_s is not None:
                new["min_episode_s"] = int(min_episode_s)
            if escalate_after_s is not None:
                new["escalate_after_s"] = int(escalate_after_s)
            row = await client.put_setting(dog_id, "sg_sensitivity", new)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "sg_sensitivity": row["value"], "version": row["version"]}

    @mcp.tool(name="set_silent_guardian", annotations=CONTROL)
    @audited("set_silent_guardian")
    async def set_silent_guardian(dog_id: str, on: bool) -> dict:
        """Turn Silent Guardian (bark watch and intervention) on or off right now. on=true puts the
        robot into Silent Guardian mode; on=false puts it in standby (idle). To run SG at a set time,
        use schedule_mission with type silent_guardian. The robot does not confirm the change, so
        report it as sent, and use get_robot_status to check the mode."""
        client, user_id = _client("set_silent_guardian")
        _check_dog(user_id, dog_id)
        try:
            res = await client.command(dog_id, "set_mode", sg_mode_command(bool(on)))
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return sg_mode_tool_result(bool(on), res)

    @mcp.tool(name="set_coach", annotations=CONTROL)
    @audited("set_coach")
    async def set_coach(dog_id: str, on: bool) -> dict:
        """Turn Coach mode (active trick training) on or off right now. on=true puts the robot into
        Coach mode; on=false puts it in standby (idle). To run Coach at a set time, use
        schedule_mission with type coach. The robot does not confirm the change, so report it as
        sent, and use get_robot_status to check the mode."""
        client, user_id = _client("set_coach")
        _check_dog(user_id, dog_id)
        try:
            res = await client.command(dog_id, "set_mode", coach_mode_command(bool(on)))
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return coach_mode_tool_result(bool(on), res)

    @mcp.tool(name="set_smart_plug", annotations=CONTROL)
    @audited("set_smart_plug")
    async def set_smart_plug(dog_id: str, action: str) -> dict:
        """Switch or check the robot's smart plug, which powers an Adaptil calming-pheromone
        diffuser. action: on, off or status. Silent Guardian switches it on by itself at a level 2+
        intervention and off 2 hours later; on/off here is a manual override, so the robot will not
        switch it off by itself afterwards. available=false means the plug is not available (it may
        be unplugged): say so plainly, it is not a fault. Setting up a plug is done in the app."""
        client, user_id = _client("set_smart_plug")
        _check_dog(user_id, dog_id)
        action = str(action or "").strip().lower()
        if action not in SMART_PLUG_ASSISTANT_ACTIONS:
            raise ToolError("action must be on, off or status")
        try:
            res = await client.command(dog_id, "smart_plug", {"action": action}, wait_s=SMART_PLUG_WAIT_S)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return smart_plug_tool_result(action, res)

    @mcp.tool(name="get_trick_history", annotations=READ)
    @audited("get_trick_history")
    async def get_trick_history(dog_id: str, days: int = 30, recent: int = 10) -> dict:
        """The dog's trick record from Coach cues over the last `days` days (1-730, default 30; today
        included): per trick attempts, successes, success_rate (0-1) / success_pct, avg_latency_ms
        and the last attempt; `recent` newest attempts (0-50, default 10); overall totals.
        Numbers only."""
        client, user_id = _client("get_trick_history")
        _check_dog(user_id, dog_id)
        try:
            return await client.trick_history(dog_id, max(1, min(int(days), 730)), max(0, min(int(recent), 50)))
        except RelayRefusal as e:
            return e.as_result(_status=e.status)

    @mcp.tool(name="set_treat_schedule", annotations=CONTROL)
    @audited("set_treat_schedule")
    async def set_treat_schedule(dog_id: str, windows: Optional[list[TreatWindowArg]] = None,
                                 default_multiplier: Optional[float] = None,
                                 daily_cap: Optional[int] = None,
                                 max_per_hour: Optional[int] = None,
                                 daily_cap_enabled: Optional[bool] = None) -> dict:
        """Change the dog's treat schedule and limits; any field you omit keeps its current value.
        windows: list of {start, end, multiplier}: start/end HH:MM 24h local (a window may wrap past
        midnight, e.g. 22:00-02:00; windows may not overlap, at most 12), multiplier 0-3 scales
        treat frequency inside the window; an empty list clears the windows. default_multiplier
        (0-3) applies outside any window. daily_cap (0-100) is the most treats per local day;
        daily_cap_enabled=false removes the daily cap (unlimited; daily_cap null in results),
        true puts it back;
        max_per_hour (0-60, 0 = no hourly limit) is the most per rolling hour. The whole treat schedule
        (limits, windows, default_multiplier) is account-wide: it applies to all the owner's dogs. Only change a limit
        when the user asks for that number. Raising a limit sends the owner a phone alert. The
        result reads back the saved schedule and the previous limits; quote them to the user."""
        client, user_id = _client("set_treat_schedule")
        _check_dog(user_id, dog_id)
        try:
            return await settings_tools.set_treat_schedule(
                client, dog_id, None if windows is None else [dict(w) for w in windows], default_multiplier,
                daily_cap, max_per_hour, daily_cap_enabled)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)

    @mcp.tool(name="set_notify_rule", annotations=CONTROL)
    @audited("set_notify_rule")
    async def set_notify_rule(dog_id: str, event_type: str, min_duration_s: Optional[float] = None,
                              severity: Optional[str] = None, attach: str = "none",
                              cooldown_s: Optional[int] = None) -> dict:
        """Add a phone notification rule for the dog. event_type: bark_episode, intervention, settle,
        pacing, departure, arrival, treat, music or mission_run. min_duration_s: only events at least
        this long. severity (bark_episode only): low, med or high, meaning at least that severe.
        attach: none, still (snapshot) or clip. cooldown_s: minimum seconds between pushes from this
        rule (0-86400). The push category (app_type) follows the event type. At most 10 rules per
        dog, counting the owner's own; editing or removing rules is done in the app. Rules never
        override quiet hours."""
        client, user_id = _client("set_notify_rule")
        _check_dog(user_id, dog_id)
        try:
            return await settings_tools.set_notify_rule(client, dog_id, event_type, min_duration_s, severity,
                                                        attach, cooldown_s)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)

    @mcp.tool(name="get_schedule", annotations=READ)
    @audited("get_schedule")
    async def get_schedule(dog_id: str) -> dict:
        """List the dog's scheduled missions and ask the robot what it has loaded (schedule_sync).
        Each mission has id, name, type, time (HH:MM, the dog's local time), days, duration_min and,
        when the robot confirms, on_robot and next_fire_at. confirmed_by_robot=false means the list is
        the relay's copy only (robot offline or no reply)."""
        client, user_id = _client("get_schedule")
        _check_dog(user_id, dog_id)
        return await schedule_tools.get_schedule(client, dog_id)

    @mcp.tool(name="schedule_mission", annotations=CONTROL)
    @audited("schedule_mission")
    async def schedule_mission(dog_id: str, type: str, time: str, days: Optional[list[str]] = None,
                               duration_min: Optional[int] = None, name: Optional[str] = None,
                               reward_treats: Optional[int] = None, goal: str = "") -> dict:
        """Schedule a mission on the robot right away (no approval step). type: a training mission
        (recall, sit_stay, down_stay, place, quiet, find_treat, sit, down, stay, spin; 1-60 min,
        default 2) or a mode: silent_guardian (bark watch) or coach (5-720 min, default 120): the robot
        enters that mode at the time and returns to idle after duration_min. time: HH:MM 24h in the
        dog's local time. days: mon..sun (default every day). reward_treats 0-3 (training only).
        Returns mission_id and the robot's confirmation: confirmed_by_robot and next_fire_at. Only say
        it is scheduled on the robot when confirmed_by_robot is true."""
        client, user_id = _client("schedule_mission")
        _check_dog(user_id, dog_id)
        return await schedule_tools.schedule_mission(client, dog_id, type, time, days, duration_min, name,
                                                     reward_treats, goal)

    @mcp.tool(name="cancel_mission", annotations=CONTROL)
    @audited("cancel_mission")
    async def cancel_mission(dog_id: str, mission_id: str) -> dict:
        """Delete a scheduled mission by mission_id (from get_schedule or get_missions). The robot is
        asked to confirm it is gone (confirmed_by_robot)."""
        client, user_id = _client("cancel_mission")
        _check_dog(user_id, dog_id)
        return await schedule_tools.cancel_mission(client, dog_id, mission_id)

    @mcp.tool(name="speak", annotations=CONTROL)
    @audited("speak")
    async def speak(dog_id: str, text: str) -> dict:
        """Make the robot say a short line out loud, in the WIM-Z voice (the only voice it has).
        text: up to 280 characters. Refused during quiet hours, and limited to 6 per minute."""
        client, user_id = _client("speak")
        _check_dog(user_id, dog_id)
        text = (text or "").strip()
        if not text or len(text) > 280:
            return {"ok": False, "refused": True, "reason": "speak_text_invalid", "max_chars": 280, "chars": len(text)}
        try:
            res = await client.speak(dog_id, text)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "chars": res.get("chars"), "delivered": res.get("delivered"),
                "simulated": res.get("simulated"), "speaks_left_this_minute": res.get("speaks_left_this_minute")}

    @mcp.tool(name="give_treat", annotations=CONTROL)
    @audited("give_treat")
    async def give_treat(dog_id: str, reason: str = "") -> dict:
        """Dispense one treat now. Subject to the owner's daily cap and hourly limit; the result says
        how many treats were given today versus the cap, or why it was refused."""
        client, user_id = _client("give_treat")
        _check_dog(user_id, dog_id)
        try:
            res = await client.command(dog_id, "dispense_treat", {"reason": reason[:120]})
        except RelayRefusal as e:
            out = e.as_result(_status=e.status)
            if e.code == "treat_cap_reached":
                out["treats_today"] = e.detail.get("used")
                out["treat_daily_cap"] = e.detail.get("cap")
            return out
        guard = res.get("guard") or {}
        return {"ok": True, "treats_today": guard.get("treats_used"), "treat_daily_cap": guard.get("treat_cap"),
                "delivered": res.get("delivered"), "simulated": res.get("simulated"),
                "command_id": res.get("command_id")}

    def _image(raw: bytes, content_type: str) -> Image:
        fmt = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}.get(content_type.split(";")[0].strip(), "jpeg")
        return Image(data=raw, format=fmt)

    @mcp.tool(name="get_event_photo", annotations=READ)
    @audited("get_event_photo")
    async def get_event_photo(dog_id: str, event_id: str) -> list:
        """The still WIM-Z already took for an event (treat, bark_episode, intervention). Use the
        event_id from get_events where has_photo is true. Returns the image; stills expire after
        24 hours (treat snapshots after 7 days). Requires the photos permission."""
        client, user_id = _client("get_event_photo")
        _check_dog(user_id, dog_id)
        try:
            raw, ctype = await client.event_photo(dog_id, event_id)
        except RelayRefusal as e:
            return [json.dumps(e.as_result(event_id=event_id))]
        return [_image(raw, ctype), json.dumps({"ok": True, "event_id": event_id, "bytes": len(raw)})]

    @mcp.tool(name="take_photo", annotations=CONTROL)
    @audited("take_photo")
    async def take_photo(dog_id: str) -> list:
        """Ask WIM-Z to take a new snapshot now (no HUD) and return it. At most one every 0.5 seconds
        per assistant, and never live video. Requires the camera permission."""
        client, user_id = _client("take_photo")
        _check_dog(user_id, dog_id)
        try:
            res = await client.command(dog_id, "take_photo", {"with_hud": False}, wait_s=12)
        except RelayRefusal as e:
            return [json.dumps(e.as_result())]
        result = (res.get("result") or {})
        inner = result.get("result") if isinstance(result.get("result"), dict) else {}
        image_url = inner.get("image_url") or inner.get("url")
        if res.get("simulated"):
            return [json.dumps({"ok": False, "reason": "robot_offline_demo", "simulated": True})]
        if res.get("timed_out") or not image_url:
            return [json.dumps({"ok": False, "reason": "photo_timed_out" if res.get("timed_out") else "no_image_returned",
                                "command_id": res.get("command_id"), "error": result.get("error")})]
        try:
            raw, ctype = await client.still(dog_id, image_url)
        except RelayRefusal as e:
            return [json.dumps(e.as_result())]
        return [_image(raw, ctype), json.dumps({"ok": True, "command_id": res.get("command_id"), "bytes": len(raw)})]

    @mcp.tool(name="listen", annotations=CONTROL)
    @audited("listen")
    async def listen(dog_id: str, seconds: int) -> list:
        """Asks the robot to listen for up to 15 seconds and returns what it heard, with the recording."""
        client, user_id = _client("listen")
        _check_dog(user_id, dog_id)
        if not 1 <= int(seconds) <= LISTEN_MAX_S:
            raise ToolError(f"seconds must be 1 to {LISTEN_MAX_S}")
        try:
            res = await client.command(dog_id, "listen", {"seconds": int(seconds)},
                                       wait_s=int(seconds) + LISTEN_EXTRA_WAIT_S)
        except RelayRefusal as e:
            return [json.dumps(e.as_result())]
        fields, clip = listen_tool_result(res)
        if not clip:
            if fields.get("ok"):
                fields["clip"] = "none_returned"
            return [json.dumps(fields)]
        try:
            raw, ctype = await client.still(dog_id, clip)
        except RelayRefusal as e:
            fields["clip"] = "unavailable"
            fields["clip_error"] = e.as_result().get("reason")
            return [json.dumps(fields)]
        fields["clip_bytes"] = len(raw)
        return [_audio(raw, ctype), json.dumps(fields)]

    def _audio(raw: bytes, content_type: str) -> Audio:
        fmt = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mpeg", "audio/mp4": "mp4",
               "audio/ogg": "ogg", "audio/flac": "flac", "audio/aac": "aac"}.get(content_type.split(";")[0].strip(), "wav")
        return Audio(data=raw, format=fmt)

    @mcp.tool(name="go_to", annotations=CONTROL)
    @audited("go_to")
    async def go_to(dog_id: str, target: str) -> dict:
        """Send the robot to the dog or back to the dock; it finishes on its own."""
        client, user_id = _client("go_to")
        _check_dog(user_id, dog_id)
        if target not in GO_TO_TARGETS:
            raise ToolError(f"target must be one of: {', '.join(GO_TO_TARGETS)}")
        try:
            res = await client.command(dog_id, "go_to", {"target": target})
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "target": target, "delivered": res.get("delivered"), "simulated": res.get("simulated"),
                "command_id": res.get("command_id")}

    @mcp.tool(name="move", annotations=CONTROL)
    @audited("move")
    async def move(dog_id: str, action: str, amount: Optional[int] = None) -> dict:
        """One small supervised step at creep speed; take a photo, decide one step, repeat."""
        client, user_id = _client("move")
        _check_dog(user_id, dog_id)
        if action not in MOVE_ACTIONS:
            raise ToolError(f"action must be one of: {', '.join(MOVE_ACTIONS)}")
        data: dict[str, Any] = {"action": action}
        if amount is not None:
            data["amount"] = amount
        try:
            res = await client.command(dog_id, "move", data, wait_s=MOVE_WAIT_S)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return _move_result(action, res)
