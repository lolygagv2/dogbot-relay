"""Notify rules and voice-agent hooks (Dec 9 demo build, 2026-11-06).

`evaluate_notify_rules` runs on every ingested event: matches the dog's `notify_rules`,
respects quiet hours and per-rule cooldowns, attaches a still or clip URL from the
event payload, and sends an FCM push through `push_service.send_push`. Every decision
is written to `notify_log` (that table also drives the cooldowns).

The relay runs no robot-action triggers of its own (the departure/arrival triggers and the
15-minute departure fallback were removed 2026-11-18): missions fire on the robot's clock or
on demand, and the robot runs every `music_plan` trigger itself.

`notify_agent` posts fire-and-forget hooks to the voice agent process.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.database import (
    get_dog_by_id,
    insert_notify_log,
    last_notify_sent,
    last_notify_sent_event_ts,
)
from app.models import NotifyRule, NotifyRules
from app.services import settings_service

logger = logging.getLogger(__name__)

# Quiet hours never hold these back (bark alerts); every other rule is non-urgent and is
# suppressed during quiet hours unless the owner set override_quiet on it.
URGENT_EVENT_TYPES = {"bark_episode"}


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


async def evaluate_notify_rules(ev: dict, user_id: Optional[str],
                                only_rule_ids: Optional[set[str]] = None) -> list[dict]:
    """Evaluate the dog's rules against one event. Returns the per-rule outcomes.
    only_rule_ids limits it to those rules (replay jump catch-up)."""
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
    from app.services.replayer import get_replayer
    replay_clock = ev.get("source") == "replay" or get_replayer().handles(dog_id)

    for rule in rules.rules:
        if not rule.enabled or rule.event_type != ev["type"] or not _matches(rule, ev):
            continue
        if only_rule_ids is not None and rule.id not in only_rule_ids:
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

        if quiet and rule.event_type not in URGENT_EVENT_TYPES and not rule.override_quiet:
            log("suppressed_quiet")
            continue
        if rule.cooldown_s:
            if replay_clock:
                # demo replay: cooldown runs on the replay clock (event time), so a jump or a
                # fast replay pushes what a real day would have, not what wall time allows
                last = last_notify_sent_event_ts(dog_id, rule.id, ev["ts"])
                age = (datetime.fromisoformat(ev["ts"]) - datetime.fromisoformat(last)).total_seconds() if last else None
            else:
                last = last_notify_sent(dog_id, rule.id)
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
            if age is not None and age < rule.cooldown_s:
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
