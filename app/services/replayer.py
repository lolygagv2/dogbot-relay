"""Demo replayer (Dec 9 demo build, 2026-11-06).

Loads a seed file from <repo>/seeds, materialises its events onto a local date for the
demo dog, and feeds them through the normal ingest pipeline at a chosen speed, so the
app, push notifications, roll-ups and the voice agent all behave as if a robot were
live. In-process singleton; admin-only routes in app/routers/replay.py.

Seed format (seeds/demo_day.json):
  {"version": 1, "name": "...", "timezone": "America/New_York", "day_start": "07:30",
   "events": [{"offset_s": 0, "type": "status", "data": {...}, "duration_s": ..., "id": "ep1"},
              {"offset_s": 2430, "type": "intervention", "data": {"kind": "sound", "episode_ref": "ep1"}}]}
`episode_ref` resolves to the generated event_id of the seed entry with that `id`.
Event ids are deterministic (replay:<dog>:<date>:<idx>) so re-runs are idempotent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.auth import Actor
from app.config import get_settings
from app.database import (
    add_user_dog,
    create_dog,
    delete_activity_events_by_ids,
    delete_daily_summaries,
    delete_events_by_ids,
    delete_events_by_source,
    delete_mission_drafts_for_dog,
    delete_notify_log_by_event_ids,
    delete_notify_log_for_dog,
    delete_treat_ledger_for_dog,
    get_dog_by_id,
    get_event,
    get_user_dog_role,
    put_dog_setting,
)
from app.services import event_store, settings_service

logger = logging.getLogger(__name__)


def seeds_dir() -> Path:
    s = get_settings()
    return Path(s.seeds_dir) if s.seeds_dir else Path(__file__).parent.parent.parent / "seeds"


class Replayer:
    def __init__(self) -> None:
        self.status = "idle"
        self.seed_name: Optional[str] = None
        self.seed: dict = {}
        self.dog_id: Optional[str] = None
        self.device_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.tz = ZoneInfo("UTC")
        self.date: Optional[str] = None
        self.day_start_s = 0
        self.speed = 60.0
        self.cursor = 0
        self.events: list[dict] = []
        self.emitted = 0
        self.started_at: Optional[str] = None
        self._vt_anchor = 0.0
        self._wall_anchor = time.monotonic()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    # ---- helpers ----

    def handles(self, dog_id: str) -> bool:
        return self.status != "idle" and dog_id == self.dog_id

    def virtual_offset(self) -> float:
        if self._running:
            return self._vt_anchor + (time.monotonic() - self._wall_anchor) * self.speed
        return self._vt_anchor

    def _anchor(self, offset: Optional[float] = None) -> None:
        self._vt_anchor = self.virtual_offset() if offset is None else offset
        self._wall_anchor = time.monotonic()

    def _day_start_dt(self) -> datetime:
        d = datetime.strptime(self.date, "%Y-%m-%d").replace(tzinfo=self.tz)
        return d + timedelta(seconds=self.day_start_s)

    def virtual_time(self) -> datetime:
        return self._day_start_dt() + timedelta(seconds=self.virtual_offset())

    def state(self) -> dict:
        return {
            "status": self.status, "seed": self.seed_name, "name": self.seed.get("name"),
            "dog_id": self.dog_id, "device_id": self.device_id, "user_id": self.user_id,
            "date": self.date, "timezone": str(self.tz.key) if hasattr(self.tz, "key") else "UTC",
            "speed": self.speed, "cursor": self.cursor, "total": len(self.events), "emitted": self.emitted,
            "virtual_time": self.virtual_time().isoformat() if self.date else None,
            "virtual_offset_s": round(self.virtual_offset()) if self.date else None,
            "next_event": (self.events[self.cursor]["event"]["type"] if self.cursor < len(self.events) else None),
            "started_at": self.started_at,
        }

    @staticmethod
    def list_seeds() -> list[str]:
        d = seeds_dir()
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []

    # ---- lifecycle ----

    async def load(self, seed_name: str, admin: Actor, dog_id: Optional[str] = None,
                   device_id: Optional[str] = None, date: Optional[str] = None) -> dict:
        await self.reset(delete_data=False)
        s = get_settings()
        path = seeds_dir() / Path(seed_name).name
        if not path.exists():
            raise HTTPException(status_code=404, detail={"code": "seed_not_found", "seed": seed_name})
        try:
            seed = json.loads(path.read_text())
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": str(e)})
        if not isinstance(seed.get("events"), list) or not seed["events"]:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": "events[] required"})

        self.seed_name, self.seed = path.name, seed
        self.dog_id = dog_id or s.demo_dog_id
        self.device_id = device_id or s.demo_device_id
        self.user_id = admin.user_id
        try:
            self.tz = ZoneInfo(seed.get("timezone") or s.default_timezone)
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": "bad timezone"})
        h, m = (seed.get("day_start") or "07:00").split(":")
        self.day_start_s = int(h) * 3600 + int(m) * 60
        self.date = date or datetime.now(timezone.utc).astimezone(self.tz).date().isoformat()
        try:
            datetime.strptime(self.date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "bad_date"})

        # demo dog under the loading admin
        if not get_dog_by_id(self.dog_id):
            create_dog(dog_id=self.dog_id, name=seed.get("dog_name") or "Demo dog", user_id=admin.user_id,
                       breed=seed.get("dog_breed"), notes="Replayer demo dog")
        if not get_user_dog_role(admin.user_id, self.dog_id):
            add_user_dog(admin.user_id, self.dog_id, "owner")
        put_dog_setting(self.dog_id, "timezone", str(self.tz.key), "replayer")
        for key, value in (seed.get("settings") or {}).items():
            try:
                put_dog_setting(self.dog_id, key, settings_service.validate_value(key, value), "replayer")
            except HTTPException as e:
                logger.warning(f"[REPLAY] seed setting {key} ignored: {e.detail}")

        # materialise
        base = self._day_start_dt()
        ref_ids: dict[str, str] = {}
        materialised = []
        entries = sorted(seed["events"], key=lambda e: float(e.get("offset_s", 0)))
        for idx, entry in enumerate(entries):
            eid = f"replay:{self.dog_id}:{self.date}:{idx}"
            if entry.get("id"):
                ref_ids[str(entry["id"])] = eid
            materialised.append({"idx": idx, "offset_s": float(entry.get("offset_s", 0)), "entry": entry, "event_id": eid})
        for item in materialised:
            entry = item["entry"]
            data = dict(entry.get("data") or {})
            ref = data.pop("episode_ref", None)
            if ref and ref in ref_ids:
                data["episode_id"] = ref_ids[ref]
            item["event"] = {
                "event_id": item["event_id"], "type": entry.get("type"), "dog_id": self.dog_id,
                "ts": (base + timedelta(seconds=item["offset_s"])).isoformat(),
                "duration_s": entry.get("duration_s"), "data": data,
            }
        self.events = materialised
        self.cursor = 0
        self.emitted = 0
        self._anchor(0.0)
        self.status = "loaded"
        logger.info(f"[REPLAY] loaded {self.seed_name}: {len(self.events)} events for {self.dog_id} on {self.date}")
        return self.state()

    def _require_loaded(self) -> None:
        if self.status == "idle":
            raise HTTPException(status_code=409, detail={"code": "replay_not_loaded"})

    async def start(self, speed: Optional[float] = None) -> dict:
        self._require_loaded()
        if speed:
            self.speed = float(speed)
        if self.status == "done":
            self.cursor = 0
            self._anchor(0.0)
        self._anchor()
        self._running = True
        self.status = "running"
        self.started_at = self.started_at or datetime.now(timezone.utc).isoformat()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        self._wake.set()
        return self.state()

    async def pause(self) -> dict:
        self._require_loaded()
        self._anchor()
        self._running = False
        if self.status == "running":
            self.status = "paused"
        self._wake.set()
        return self.state()

    async def set_speed(self, speed: float) -> dict:
        self._require_loaded()
        self._anchor()
        self.speed = float(speed)
        self._wake.set()
        return self.state()

    async def jump(self, offset_s: Optional[float] = None, to: Optional[str] = None) -> dict:
        self._require_loaded()
        if to:
            h, m = to.split(":")
            offset_s = int(h) * 3600 + int(m) * 60 - self.day_start_s
        if offset_s is None:
            raise HTTPException(status_code=422, detail={"code": "jump_target_required"})
        offset_s = max(0.0, float(offset_s))
        catch_up: list[dict] = []
        was_running = self._running
        self._running = False
        self._anchor()
        k = next((i for i, e in enumerate(self.events) if e["offset_s"] > offset_s), len(self.events))
        if k < self.cursor:
            # backward: drop everything already emitted at/after the new cursor
            ids = [e["event_id"] for e in self.events[k:self.cursor]]
            delete_events_by_ids(ids)
            delete_activity_events_by_ids(ids)
            delete_notify_log_by_event_ids(ids)
            delete_daily_summaries(self.dog_id)
            self.emitted = max(0, self.emitted - len(ids))
            # the robot's remembered mode goes back to what it was at the new cursor
            event_store.set_last_mode(self.device_id, next(
                (m for m in (str((e["event"]["data"] or {}).get("mode") or "").strip().lower()
                             for e in reversed(self.events[:k]) if e["event"]["type"] == "status") if m), None))
        elif k > self.cursor:
            # forward: insert silently (no per-event push or WS frames), then fire only what the
            # newest skipped events would have fired, so the jump lands with the right push
            # and spoken line instead of silence
            batch = [e["event"] for e in self.events[self.cursor:k]]
            res = await event_store.ingest_events(batch, device_id=self.device_id, user_id=self.user_id,
                                                  source="replay", notify=False, forward=False)
            self.emitted += len(res["accepted"])
            stored = [ev for ev in (get_event(i) for i in res["accepted"]) if ev]
            catch_up = await event_store.catch_up_skipped(
                stored, [e["event"] for e in self.events[:self.cursor]],
                device_id=self.device_id, user_id=self.user_id)
        self.cursor = k
        self._anchor(offset_s)
        self.status = "done" if self.cursor >= len(self.events) and not was_running else ("running" if was_running else "paused")
        await self._send_apps({"type": "replay_jump", "dog_id": self.dog_id,
                               "virtual_time": self.virtual_time().isoformat()})
        if was_running and self.cursor < len(self.events):
            self._running = True
            self.status = "running"
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        self._wake.set()
        if catch_up:
            logger.info(f"[REPLAY] jump catch-up: {[c['kind'] + ':' + c.get('type', '') for c in catch_up]}")
        return {**self.state(), "catch_up": catch_up}

    async def reset(self, delete_data: bool = True) -> dict:
        self._running = False
        self._wake.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if delete_data and self.dog_id:
            ids = delete_events_by_source(self.dog_id, "replay")
            ids += delete_events_by_source(self.dog_id, "relay")
            delete_activity_events_by_ids(ids)
            delete_notify_log_for_dog(self.dog_id)
            delete_treat_ledger_for_dog(self.dog_id)
            delete_daily_summaries(self.dog_id)
            delete_mission_drafts_for_dog(self.dog_id)
            await self._send_apps({"type": "replay_reset", "dog_id": self.dog_id})
            logger.info(f"[REPLAY] reset: removed {len(ids)} events for {self.dog_id}")
        self.status = "idle"
        self.cursor = 0
        self.emitted = 0
        self.started_at = None
        self._anchor(0.0)
        return self.state()

    # ---- loop ----

    async def _run(self) -> None:
        try:
            while self.cursor < len(self.events):
                if not self._running:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                ev = self.events[self.cursor]
                wait = (ev["offset_s"] - self.virtual_offset()) / max(self.speed, 0.001)
                if wait > 0:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                        continue  # speed/jump/pause changed -> recompute
                    except asyncio.TimeoutError:
                        pass
                res = await event_store.ingest_events([ev["event"]], device_id=self.device_id,
                                                      user_id=self.user_id, source="replay")
                self.emitted += len(res["accepted"])
                if res["rejected"]:
                    logger.warning(f"[REPLAY] seed event {ev['idx']} rejected: {res['rejected']}")
                self.cursor += 1
            self._running = False
            self.status = "done"
            logger.info(f"[REPLAY] done: {self.emitted} events emitted for {self.dog_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[REPLAY] loop crashed: {e}")
            self._running = False
            self.status = "paused"

    # ---- command simulation (demo dog with no live robot) ----

    async def simulate_command(self, dog_id: str, message: dict, actor: Actor) -> None:
        cmd, data = message.get("command"), message.get("data") or {}
        now = self.virtual_time() if self.status != "idle" else datetime.now(timezone.utc)
        src = {"app": "app", "admin": "app", "connector": "connector", "agent": "agent",
               "relay": "schedule"}.get(actor.kind, "relay")
        events: list[dict] = []
        if cmd == "dispense_treat":
            events.append({"type": "treat", "ts": now.isoformat(),
                           "data": {"source": src, "command_id": data.get("command_id"), "count": 1,
                                    "multiplier": data.get("multiplier", 1.0)}})
        elif cmd == "audio":
            events.append({"type": "intervention", "ts": now.isoformat(),
                           "data": {"kind": "sound", "sound_id": data.get("sound_id"), "result": "no_change"}})
        elif cmd == "play_music":
            events.append({"type": "music", "ts": now.isoformat(),
                           "data": {"action": "start", "track": data.get("track"), "trigger": data.get("trigger")}})
        elif cmd == "start_mission":
            events.append({"type": "mission_run", "ts": (now + timedelta(seconds=3)).isoformat(),
                           "duration_s": 60,
                           "data": {"mission_id": data.get("mission_id"), "mission_type": data.get("mission_type"),
                                    "result": "success", "trigger": data.get("trigger") or src}})
        elif cmd == "set_mode":
            events.append({"type": "status", "ts": now.isoformat(),
                           "data": {"battery": 80, "mode": data.get("mode"), "treats_remaining": 8}})
        elif cmd == "go_to":
            events.append({"type": "status", "ts": now.isoformat(),
                           "data": {"battery": 80, "mode": "mission", "location": data.get("target")}})
        for e in events:
            e["event_id"] = f"relay:{uuid.uuid4()}"
            e["dog_id"] = dog_id
        if events:
            await event_store.ingest_events(events, device_id=self.device_id or get_settings().demo_device_id,
                                            user_id=self.user_id, source="relay")

    async def _send_apps(self, msg: dict) -> None:
        try:
            from app.connection_manager import get_connection_manager
            if self.user_id:
                await get_connection_manager().send_to_user_apps(self.user_id, msg)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[REPLAY] app notify failed: {e}")


_replayer: Optional[Replayer] = None


def get_replayer() -> Replayer:
    global _replayer
    if _replayer is None:
        _replayer = Replayer()
    return _replayer
