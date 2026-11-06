"""Notify rules, departure triggers and voice-agent hooks (Dec 9 demo build, 2026-11-06).

`evaluate_notify_rules` runs on every ingested event: matches the dog's `notify_rules`,
respects quiet hours and per-rule cooldowns, attaches a still or clip URL from the
event payload, and sends an FCM push through `push_service.send_push`. Every decision
is written to `notify_log` (that table also drives the cooldowns).

`handle_triggers` arms delayed robot actions on `departure` (missions with a
`departure` trigger, `music_plan.on_departure`) and cancels them on `arrival`.

`notify_agent` posts fire-and-forget hooks to the voice agent process.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.database import (
    get_dog_by_id,
    get_latest_event,
    insert_notify_log,
    last_notify_sent,
)
from app.models import Missions, MusicPlan, NotifyRule, NotifyRules
from app.services import settings_service

logger = logging.getLogger(__name__)

# dog_id -> list of pending departure-trigger tasks
_departure_tasks: dict[str, list[asyncio.Task]] = {}


# ---------------------------------------------------------------------------
# Notify rules
# ---------------------------------------------------------------------------

class _Safe(dict):
    def __missing__(self, key):
        return "?"


def _fmt_vars(ev: dict, dog_name: str) -> dict:
    d = ev["data"]
    dur = ev.get("duration_s") or 0
    settle = d.get("settle_s") or dur
    return _Safe(
        dog_name=dog_name, duration_s=round(dur), duration_min=max(1, round(dur / 60)) if dur else 0,
        settle_min=max(1, round(settle / 60)) if settle else 0, bark_count=d.get("bark_count", "?"),
        severity=d.get("severity", "?"), kind=d.get("kind", "?"), result=d.get("result", "?"),
        mission_type=d.get("mission_type") or d.get("mission_id") or "?", source=d.get("source", "?"),
        track=d.get("track", "?"),
    )


def _matches(rule: NotifyRule, ev: dict) -> bool:
    w, d, dur = rule.when, ev["data"], ev.get("duration_s")
    if w.min_duration_s is not None and (dur is None or dur < w.min_duration_s):
        return False
    if w.max_duration_s is not None and (dur is None or dur > w.max_duration_s):
        return False
    if w.severity_in and d.get("severity") not in w.severity_in:
        return False
    if w.result_in and d.get("result") not in w.result_in:
        return False
    if w.source_in and d.get("source") not in w.source_in:
        return False
    if w.kind_in and d.get("kind") not in w.kind_in:
        return False
    if w.data_eq:
        for k, v in w.data_eq.items():
            if d.get(k) != v:
                return False
    return True


async def evaluate_notify_rules(ev: dict, user_id: Optional[str]) -> list[dict]:
    """Evaluate the dog's rules against one event. Returns the per-rule outcomes."""
    from app.services import push_service

    dog_id = ev["dog_id"]
    if ev["type"] == "status":
        return []
    rules = NotifyRules.model_validate(settings_service.get_value(dog_id, "notify_rules"))
    outcomes = []
    dog = get_dog_by_id(dog_id)
    dog_name = (dog or {}).get("name") or "Your dog"
    local_ts = (datetime.fromisoformat(ev["ts"]).astimezone(settings_service.get_timezone(dog_id)))
    quiet = settings_service.in_quiet_hours(dog_id, local_ts)

    for rule in rules.rules:
        if not rule.enabled or rule.event_type != ev["type"] or not _matches(rule, ev):
            continue
        image = ev["data"].get("image_url") if rule.attach == "still" else None
        clip = ev["data"].get("clip_url") if rule.attach == "clip" else None
        variables = _fmt_vars(ev, dog_name)
        title = rule.title.format_map(variables)
        body = rule.body.format_map(variables)

        def log(outcome: str) -> None:
            insert_notify_log(dog_id, user_id, rule.id, ev["event_id"], ev["type"], outcome,
                              title, body, image, clip)
            outcomes.append({"rule_id": rule.id, "outcome": outcome})

        if quiet and not rule.override_quiet:
            log("suppressed_quiet")
            continue
        if rule.cooldown_s:
            last = last_notify_sent(dog_id, rule.id)
            if last:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                if age < rule.cooldown_s:
                    log("suppressed_cooldown")
                    continue
        if not user_id:
            log("no_tokens")
            continue
        public_base = get_settings().public_base_url.rstrip("/")
        outcome = await push_service.send_push(
            user_id=user_id, app_type=rule.app_type, title=title, body=body,
            data={"type": rule.app_type, "dog_id": dog_id, "event_id": ev["event_id"], "rule_id": rule.id,
                  "image_url": image or "", "clip_url": clip or ""},
            image_url=(public_base + image) if image else None,
        )
        log(outcome)
    return outcomes


# ---------------------------------------------------------------------------
# Departure / arrival triggers
# ---------------------------------------------------------------------------

async def handle_triggers(ev: dict, user_id: Optional[str]) -> None:
    dog_id = ev["dog_id"]
    if ev["type"] == "arrival":
        cancel_departure_triggers(dog_id)
        return
    if ev["type"] != "departure":
        return
    cancel_departure_triggers(dog_id)
    tasks: list[asyncio.Task] = []

    missions = Missions.model_validate(settings_service.get_value(dog_id, "missions"))
    for m in missions.missions:
        if not m.enabled or m.trigger.kind != "departure":
            continue
        last = get_latest_event(dog_id, "mission_run")
        if last and last["data"].get("mission_id") == m.id:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds()
            if age < m.trigger.cooldown_s:
                logger.info(f"[TRIGGER] mission {m.id} for {dog_id} skipped (cooldown)")
                continue
        tasks.append(asyncio.create_task(_delayed_command(
            dog_id, user_id, m.trigger.delay_s, "start_mission",
            {"mission_id": m.id, "mission_type": m.type, "trigger": "departure"},
        )))

    plan = MusicPlan.model_validate(settings_service.get_value(dog_id, "music_plan"))
    if plan.enabled and plan.on_departure:
        tasks.append(asyncio.create_task(_delayed_command(
            dog_id, user_id, plan.on_departure.delay_s, "play_music",
            {"track": plan.on_departure.track, "duration_min": plan.on_departure.duration_min,
             "volume": plan.volume, "trigger": "departure"},
        )))
    if tasks:
        _departure_tasks[dog_id] = tasks


def cancel_departure_triggers(dog_id: str) -> int:
    tasks = _departure_tasks.pop(dog_id, [])
    for t in tasks:
        t.cancel()
    return len(tasks)


async def _delayed_command(dog_id: str, user_id: Optional[str], delay_s: int, command: str, data: dict) -> None:
    try:
        # During a replay, virtual time runs `speed` times faster than the wall clock.
        try:
            from app.services.replayer import get_replayer
            rp = get_replayer()
            if rp.handles(dog_id):
                delay_s = delay_s / max(rp.speed, 1.0)
        except ImportError:
            pass
        await asyncio.sleep(delay_s)
        from app.auth import Actor
        from app.services import robot_control

        actor = Actor(kind="relay", id="trigger", user_id=user_id)
        res = await robot_control.dispatch(dog_id, command, data, actor, device_id=None, allow_relay=True)
        logger.info(f"[TRIGGER] {command} for {dog_id}: {res.get('delivered')} simulated={res.get('simulated')}")
    except asyncio.CancelledError:
        logger.info(f"[TRIGGER] {command} for {dog_id} cancelled (arrival)")
    except Exception as e:
        logger.warning(f"[TRIGGER] {command} for {dog_id} failed: {e}")


# ---------------------------------------------------------------------------
# Voice-agent hooks (relay -> wimz-agent, fire-and-forget)
# ---------------------------------------------------------------------------

def notify_agent(event: str, device_id: Optional[str], user_id: Optional[str], dog_id: Optional[str],
                 payload: Optional[dict] = None, event_id: Optional[str] = None) -> None:
    settings = get_settings()
    if not settings.internal_api_token or not settings.agent_base_url:
        return
    body = {
        "event_id": event_id or f"{event}:{device_id}:{int(datetime.now(timezone.utc).timestamp())}",
        "event": event, "device_id": device_id, "user_id": user_id, "dog_id": dog_id,
        "payload": payload or {}, "ts": datetime.now(timezone.utc).isoformat(),
    }

    async def _post() -> None:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(
                    f"{settings.agent_base_url.rstrip('/')}/agent/hooks/robot-event",
                    headers={"Authorization": f"Bearer {settings.internal_api_token}"},
                    json=body,
                )
        except Exception as e:
            logger.debug(f"[AGENT-HOOK] {event} not delivered: {e}")

    try:
        asyncio.get_running_loop().create_task(_post())
    except RuntimeError:
        pass
