"""MCP tools exposed to outside assistants (Dec 9 demo build, 2026-11-06).

Read tools: get_dogs, get_day_summary, get_events, get_trend, get_missions, get_robot_status
Control tools: set_quiet_hours, set_music, draft_mission, schedule_mission, give_treat, go_to

Every tool: checks the token's scope, checks the user's role on the dog locally, then
calls the relay through RelayClient (actor `connector:<client_id>`). Relay refusals come
back as ordinary results `{ok: false, refused: true, reason, ...}` so the assistant can
tell the user why. Nothing here returns media, video or human audio; event payloads are
whitelisted to scalar fields.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.database import get_dog_by_id, get_user_dog_role, get_user_dogs
from app.mcp_server.authz import audited, require_scope
from app.services.relay_client import RelayClient, RelayError, RelayRefusal

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

EVENT_TYPES = ["bark_episode", "intervention", "settle", "pacing", "departure", "arrival", "treat", "music",
               "mission_run", "status"]
MISSION_TYPES = ["recall", "sit_stay", "down_stay", "place", "quiet", "find_treat", "sit", "down", "stay", "spin"]
GO_TO_TARGETS = ["dock", "bed", "kitchen", "door", "crate", "living_room", "owner"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
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
        alone time, music), time-to-settle stats and the top 3 bark episodes.
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
            events.append({"event_id": ev["event_id"], "type": ev["type"], "ts": ev["ts"],
                           "duration_s": ev.get("duration_s"), "source": ev.get("source"),
                           "data": _scalar(ev.get("data") or {})})
        return {"dog_id": dog_id, "events": events, "count": len(events)}

    @mcp.tool(name="get_trend", annotations=READ)
    @audited("get_trend")
    async def get_trend(dog_id: str, days: int = 7) -> dict:
        """Day-by-day series over the last 7 or 14 days (treats, bark episodes, bark seconds,
        interventions, missions, alone time, median time-to-settle) with totals and the delta
        versus the previous period of the same length. Numbers only."""
        client, user_id = _client("get_trend")
        _check_dog(user_id, dog_id)
        days = 14 if int(days) >= 14 else 7
        try:
            status = await client.get_status(dog_id)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        tz = ZoneInfo(status.get("timezone") or "UTC")
        today = datetime.now(timezone.utc).astimezone(tz).date()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(2 * days)]
        sem = asyncio.Semaphore(6)

        async def one(d: str) -> dict:
            async with sem:
                try:
                    return await client.get_summary(dog_id, d)
                except (RelayRefusal, RelayError):
                    return {"date": d, "counts": {}, "totals": {}, "time_to_settle": {}}

        summaries = await asyncio.gather(*(one(d) for d in dates))

        def row(s: dict) -> dict:
            c, t, tts = s.get("counts", {}), s.get("totals", {}), s.get("time_to_settle", {})
            return {"date": s.get("date"), "treats": t.get("treats", 0), "bark_episodes": c.get("bark_episode", 0),
                    "bark_s": t.get("bark_s", 0), "interventions": c.get("intervention", 0),
                    "pacing_s": t.get("pacing_s", 0), "alone_s": t.get("alone_s", 0),
                    "missions_success": t.get("missions_success", 0), "missions_fail": t.get("missions_fail", 0),
                    "settle_median_s": tts.get("median_s")}

        rows = [row(s) for s in summaries]
        current, previous = list(reversed(rows[:days])), list(reversed(rows[days:]))
        metrics = ["treats", "bark_episodes", "bark_s", "interventions", "pacing_s", "alone_s",
                   "missions_success", "missions_fail"]

        def totals(rs: list[dict]) -> dict:
            out = {m: sum((r.get(m) or 0) for r in rs) for m in metrics}
            med = [r["settle_median_s"] for r in rs if r.get("settle_median_s") is not None]
            out["settle_median_s_avg"] = round(sum(med) / len(med)) if med else None
            return out

        cur_t, prev_t = totals(current), totals(previous)
        delta = {m: (cur_t[m] - prev_t[m]) for m in metrics}
        return {"dog_id": dog_id, "days": days, "from": current[0]["date"], "to": current[-1]["date"],
                "series": current, "totals": cur_t, "previous_period_totals": prev_t, "delta": delta}

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
                "battery_pct": st.get("battery"), "treats_remaining_in_hopper": last.get("treats_remaining"),
                "treats_today": st.get("treats_today"), "treat_daily_cap": st.get("treat_cap"),
                "quiet_hours": st.get("quiet_hours"), "timezone": st.get("timezone"),
                "last_seen": st.get("last_status_ts"), "demo_replay_active": st.get("replay_active")}

    # --------------------------------------------------------------- control

    @mcp.tool(name="set_quiet_hours", annotations=CONTROL)
    @audited("set_quiet_hours")
    async def set_quiet_hours(dog_id: str, start: str, end: str) -> dict:
        """Set the dog's quiet hours window (no pushes, no agent speech) as HH:MM 24h local times,
        e.g. start "22:00" end "07:00". Assistants may only WIDEN the window; narrowing or
        disabling must be done by the owner in the app."""
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

    def _normalise_draft(dog_id: str, goal: str, type: str, times: list[str], days: list[str],
                         trigger: str, duration_s: int, reward_treats: int, name: Optional[str]) -> tuple[dict, list[str]]:
        warnings: list[str] = []
        if type not in MISSION_TYPES:
            raise ToolError(f"type must be one of: {', '.join(MISSION_TYPES)}")
        trigger = trigger if trigger in ("schedule", "departure", "manual") else "schedule"
        times = [t for t in (times or []) if _hm_ok(t)]
        if trigger == "schedule" and not times:
            raise ToolError("A scheduled mission needs at least one time (HH:MM).")
        if len(times) > 1:
            warnings.append("Only the first time is used for a single mission; draft one mission per time.")
        days = [d for d in (days or []) if d in DAYS] or DAYS
        reward_treats = max(0, min(int(reward_treats), 3))
        duration_s = max(10, min(int(duration_s), 3600))
        slug = re.sub(r"[^a-z0-9]+", "_", (name or type).lower()).strip("_")[:24] or type
        draft = {
            "id": f"m_{slug}_{secrets.token_hex(2)}",
            "type": type,
            "name": (name or f"{type.replace('_', ' ').title()} mission")[:120],
            "enabled": True,
            "trigger": {"kind": trigger, "time": times[0] if times else None, "delay_s": 300,
                        "cooldown_s": 7200, "days": days},
            "duration_s": duration_s,
            "reward_treats": reward_treats,
            "goal": (goal or "")[:240],
        }
        canonical = json.dumps({k: v for k, v in draft.items() if k != "id"}, sort_keys=True)
        draft["draft_hash"] = hashlib.sha256((dog_id + canonical).encode()).hexdigest()[:16]
        return draft, warnings

    @mcp.tool(name="draft_mission", annotations=READ)
    @audited("draft_mission")
    async def draft_mission(dog_id: str, goal: str, type: str, times: Optional[list[str]] = None,
                            days: Optional[list[str]] = None, trigger: str = "schedule",
                            duration_s: int = 120, reward_treats: int = 1, name: Optional[str] = None) -> dict:
        """Draft (but do not save) a training mission for review. type: recall, sit_stay, down_stay,
        place, quiet, find_treat, sit, down, stay, spin. trigger: schedule (needs times ["HH:MM"]),
        departure (runs after the owner leaves) or manual. Returns a normalised draft with warnings;
        pass the draft object unchanged to schedule_mission to save it."""
        client, user_id = _client("draft_mission")
        _check_dog(user_id, dog_id)
        draft, warnings = _normalise_draft(dog_id, goal, type, times or [], days or [], trigger,
                                           duration_s, reward_treats, name)
        try:
            existing = ((await client.get_setting(dog_id, "missions")).get("value") or {}).get("missions", [])
            cap = ((await client.get_setting(dog_id, "treat_schedule")).get("value") or {}).get("daily_cap")
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        for m in existing:
            if m.get("trigger", {}).get("kind") == "schedule" and draft["trigger"]["kind"] == "schedule" \
                    and m["trigger"].get("time") == draft["trigger"]["time"]:
                warnings.append(f"Another mission ({m.get('name') or m['id']}) already runs at {draft['trigger']['time']}.")
        if cap is not None and draft["reward_treats"] and len(existing) * draft["reward_treats"] >= cap:
            warnings.append(f"Mission rewards may hit the daily treat cap ({cap}).")
        return {"draft": draft, "warnings": warnings, "ready_to_schedule": True}

    @mcp.tool(name="schedule_mission", annotations=CONTROL)
    @audited("schedule_mission")
    async def schedule_mission(dog_id: str, draft: dict) -> dict:
        """Save a mission previously returned by draft_mission (pass the draft object unchanged)."""
        client, user_id = _client("schedule_mission")
        _check_dog(user_id, dog_id)
        if not isinstance(draft, dict) or "draft_hash" not in draft:
            raise ToolError("Pass the exact draft object returned by draft_mission.")
        check, _ = _normalise_draft(dog_id, draft.get("goal", ""), draft.get("type", ""),
                                    [draft.get("trigger", {}).get("time")] if draft.get("trigger", {}).get("time") else [],
                                    draft.get("trigger", {}).get("days") or [], draft.get("trigger", {}).get("kind", "schedule"),
                                    draft.get("duration_s", 120), draft.get("reward_treats", 1), draft.get("name"))
        if check["draft_hash"] != draft["draft_hash"]:
            raise ToolError("The draft was modified after drafting; call draft_mission again.")
        mission = {k: v for k, v in draft.items() if k != "draft_hash"}
        try:
            setting = await client.get_setting(dog_id, "missions")
            value = setting.get("value") or {"missions": []}
            value["missions"] = [m for m in value.get("missions", []) if m.get("id") != mission["id"]] + [mission]
            row = await client.put_setting(dog_id, "missions", value, expected_version=setting.get("version"))
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "mission_id": mission["id"], "trigger": mission["trigger"], "version": row["version"]}

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

    @mcp.tool(name="go_to", annotations=CONTROL)
    @audited("go_to")
    async def go_to(dog_id: str, target: str) -> dict:
        """Send the robot to a named spot: dock, bed, kitchen, door, crate, living_room or owner."""
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
