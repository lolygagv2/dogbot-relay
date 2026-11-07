"""MCP tools exposed to outside assistants (Dec 9 demo build, 2026-11-06).

Read tools: get_dogs, get_day_summary, get_events, get_trend, get_missions, get_robot_status
Control tools: set_quiet_hours, set_music, draft_mission, schedule_mission, give_treat, go_to, move, speak
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
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
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

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

EVENT_TYPES = ["bark_episode", "intervention", "settle", "pacing", "departure", "arrival", "treat", "music",
               "mission_run", "status"]
MISSION_TYPES = ["recall", "sit_stay", "down_stay", "place", "quiet", "find_treat", "sit", "down", "stay", "spin"]
GO_TO_TARGETS = ["dog", "dock"]
MOVE_ACTIONS = list(_MOVE_SPEC)
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
            data = ev.get("data") or {}
            events.append({"event_id": ev["event_id"], "type": ev["type"], "ts": ev["ts"],
                           "duration_s": ev.get("duration_s"), "source": ev.get("source"),
                           "has_photo": bool(data.get("image_url")),   # fetch with get_event_photo (photos scope)
                           "data": _scalar(data)})
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
        return draft, warnings

    @mcp.tool(name="draft_mission", annotations=CONTROL)
    @audited("draft_mission")
    async def draft_mission(dog_id: str, goal: str, type: str, times: Optional[list[str]] = None,
                            days: Optional[list[str]] = None, trigger: str = "schedule",
                            duration_s: int = 120, reward_treats: int = 1, name: Optional[str] = None) -> dict:
        """Propose a training mission. It is saved as a PENDING draft and shown to the owner in the
        WIM-Z app, who approves or declines it there; nothing runs until approved. type: recall,
        sit_stay, down_stay, place, quiet, find_treat, sit, down, stay, spin. trigger: schedule
        (needs times ["HH:MM"]), departure (runs after the owner leaves) or manual. Returns draft_id."""
        client, user_id = _client("draft_mission")
        _check_dog(user_id, dog_id)
        draft, warnings = _normalise_draft(dog_id, goal, type, times or [], days or [], trigger,
                                           duration_s, reward_treats, name)
        try:
            existing = ((await client.get_setting(dog_id, "missions")).get("value") or {}).get("missions", [])
            cap = ((await client.get_setting(dog_id, "treat_schedule")).get("value") or {}).get("daily_cap")
            for m in existing:
                if m.get("trigger", {}).get("kind") == "schedule" and draft["trigger"]["kind"] == "schedule" \
                        and m["trigger"].get("time") == draft["trigger"]["time"]:
                    warnings.append(f"Another mission ({m.get('name') or m['id']}) already runs at {draft['trigger']['time']}.")
            if cap is not None and draft["reward_treats"] and len(existing) * draft["reward_treats"] >= cap:
                warnings.append(f"Mission rewards may hit the daily treat cap ({cap}).")
            row = await client.create_draft(dog_id, draft, warnings)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        return {"ok": True, "draft_id": row["draft_id"], "status": row["status"], "draft": row["draft"],
                "warnings": warnings,
                "message": "Sent to the owner's WIM-Z app for approval. Call schedule_mission with this draft_id to check."}

    @mcp.tool(name="schedule_mission", annotations=CONTROL)
    @audited("schedule_mission")
    async def schedule_mission(dog_id: str, draft_id: str) -> dict:
        """Confirm a drafted mission. Only a draft the owner has APPROVED in the WIM-Z app can be
        scheduled (approval itself schedules it). Pending drafts return refused=true with
        reason awaiting_owner_approval; declined drafts cannot be scheduled."""
        client, user_id = _client("schedule_mission")
        _check_dog(user_id, dog_id)
        try:
            d = await client.get_draft(dog_id, draft_id)
        except RelayRefusal as e:
            return e.as_result(_status=e.status)
        if d["status"] == "approved":
            return {"ok": True, "scheduled": True, "mission_id": d.get("mission_id"), "draft_id": draft_id,
                    "trigger": (d.get("draft") or {}).get("trigger")}
        if d["status"] == "pending":
            return {"ok": False, "refused": True, "reason": "awaiting_owner_approval", "draft_id": draft_id,
                    "hint": "The owner approves mission drafts in the WIM-Z app."}
        return {"ok": False, "refused": True, "reason": f"draft_{d['status']}", "draft_id": draft_id}

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
