"""Demo replayer (Dec 9 demo build, 2026-11-06).

Loads a seed file from <repo>/seeds, materialises its events onto a local date for one of
the admin's own dogs, and feeds them through the normal ingest pipeline at a chosen speed,
so the app, push notifications, roll-ups and the voice agent all behave as if a robot were
live. It only records events: it never sends the robot a command. In-process singleton;
admin-only routes in app/routers/replay.py.

Per-dog targets (2026-11-18): every call names its dog, the dog must be one the admin owns
(no placeholder dog is created), and a call for a dog other than the loaded one gets 409
replay_other_dog. Load never changes the dog's settings and uses the dog's own timezone.
A seed with "land_previous_day": true (demo_good_day) is also written in full, silently,
onto the day before the replay date, so yesterday's recap exists while today replays.
Load clears earlier replay data on the dates it writes; reset clears all of the dog's
replay-made data (events of source replay/relay and the rows linked to them, nothing else).

Seed format (seeds/demo_day.json):
  {"version": 1, "name": "...", "timezone": "America/New_York", "day_start": "07:30",
   "events": [{"offset_s": 0, "type": "status", "data": {...}, "duration_s": ..., "id": "ep1"},
              {"offset_s": 2430, "type": "intervention", "data": {"kind": "sound", "episode_ref": "ep1"}}]}
`episode_ref` resolves to the generated event_id of the seed entry with that `id`.
Media (2026-11-18): an entry may name `"still": "<file>"` and/or `"clip": "<file>"` in
seeds/media; load links them as data.image_url / data.clip_url (so has_photo is true and a rule's
attach still/clip works). A named file that is not there is left off and listed in the load
result's `media_missing`; drop the file in and load again.
Event ids are deterministic (replay:<dog>:<date>:<idx>) so re-runs are idempotent.
`day_start` is local time in the dog's timezone; the seed's `timezone` is only a fallback.
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
    delete_activity_events_by_ids,
    delete_daily_summaries,
    delete_events_by_ids,
    delete_notify_log_by_event_ids,
    delete_replay_events,
    delete_treat_ledger_by_event_ids,
    get_device_owner,
    get_dog_by_id,
    get_dog_owner,
    get_event,
    get_user_dog_role,
)
from app.services import event_store, media_files, settings_service

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
            "device_ids": self._dog_devices(),   # every robot that shows this replay
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

    def state_for(self, dog_id: str) -> dict:
        """State as seen from one dog: its own replay, or idle with busy_with naming the dog a
        replay is loaded for, so an app on another dog is never shown someone else's clock."""
        if self.handles(dog_id):
            return {**self.state(), "busy_with": None}
        busy = ({"dog_id": self.dog_id, "status": self.status, "seed": self.seed_name}
                if self.status != "idle" else None)
        return {"status": "idle", "seed": None, "name": None, "dog_id": dog_id, "device_id": None,
                "date": None, "virtual_time": None, "virtual_offset_s": None, "emitted": 0, "total": 0,
                "busy_with": busy}

    @staticmethod
    def _check_dog(admin: Actor, dog_id: str) -> None:
        if not get_dog_by_id(dog_id):
            raise HTTPException(status_code=404, detail={"code": "dog_not_found", "dog_id": dog_id})
        if get_user_dog_role(admin.user_id, dog_id) != "owner":
            raise HTTPException(status_code=403, detail={"code": "not_dog_owner", "dog_id": dog_id})

    def _require_target(self, dog_id: Optional[str]) -> None:
        if self.status == "idle":
            raise HTTPException(status_code=409, detail={"code": "replay_not_loaded", "dog_id": dog_id})
        if dog_id is not None and dog_id != self.dog_id:
            raise HTTPException(status_code=409, detail={
                "code": "replay_other_dog", "dog_id": dog_id, "loaded_dog_id": self.dog_id,
                "status": self.status, "seed": self.seed_name,
                "hint": "load a seed for this dog (that stops the other replay) or reset it"})

    @staticmethod
    def _resolve_device(dog_id: str, device_id: Optional[str], seed_device: Optional[str] = None) -> str:
        """Robot the replay's events are stamped with. A replay belongs to the dog, not to one
        robot: every robot of the owner's sees it (state device_ids, roll-ups, status) and agent
        lines go to whichever is online. Picks the body's robot (must not belong to someone else),
        else one of the dog's robots that is online, else the seed's robot if it is one of the
        dog's, else the dog's first robot, else the demo id (2026-11-19)."""
        owner = get_dog_owner(dog_id)
        if device_id:
            dev_owner = get_device_owner(device_id)
            if dev_owner and dev_owner != owner:
                raise HTTPException(status_code=422, detail={"code": "device_not_dogs", "device_id": device_id})
            return device_id
        from app.services import pairing
        devices = pairing.dog_devices(dog_id, owner)
        try:
            from app.connection_manager import get_connection_manager
            manager = get_connection_manager()
            online = next((d for d in devices if manager.is_robot_online(d)), None)
        except Exception:  # pragma: no cover
            online = None
        if online:
            return online
        if seed_device and seed_device in devices:
            return seed_device
        return devices[0] if devices else get_settings().demo_device_id

    def _dog_devices(self) -> list[str]:
        if not self.dog_id:
            return []
        from app.services import pairing
        return pairing.dog_devices(self.dog_id)

    @staticmethod
    def _link_media(entries: list[dict]) -> tuple[list[dict], list[dict]]:
        """Seed entries with still/clip file names turned into image_url/clip_url (copies)."""
        out, missing = [], []
        kinds = (("still", "image_url", media_files.IMAGE_EXTS), ("clip", "clip_url", media_files.VIDEO_EXTS))
        for entry in entries:
            if not any(entry.get(k) for k, _, _ in kinds):
                out.append(entry)
                continue
            entry = {**entry, "data": dict(entry.get("data") or {})}
            for key, field, exts in kinds:
                name = entry.get(key)
                if not name:
                    continue
                url = media_files.find_seed_media(name, exts)
                if url:
                    entry["data"][field] = url
                else:
                    missing.append({"file": name, "kind": key, "type": entry.get("type"),
                                    "offset_s": entry.get("offset_s")})
            out.append(entry)
        return out, missing

    def _materialise(self, entries: list[dict], date: str) -> list[dict]:
        base = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=self.tz) + timedelta(seconds=self.day_start_s)
        ref_ids: dict[str, str] = {}
        out = []
        for idx, entry in enumerate(entries):
            eid = f"replay:{self.dog_id}:{date}:{idx}"
            if entry.get("id"):
                ref_ids[str(entry["id"])] = eid
            out.append({"idx": idx, "offset_s": float(entry.get("offset_s", 0)), "entry": entry, "event_id": eid})
        for item in out:
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
        return out

    async def _purge(self, dog_id: str, dates: Optional[list[str]] = None) -> int:
        """Delete the dog's replay-made events (on `dates`, or all) and the rows linked to them."""
        ids = delete_replay_events(dog_id, dates)
        delete_activity_events_by_ids(ids)
        delete_notify_log_by_event_ids(ids)
        delete_treat_ledger_by_event_ids(ids)
        delete_daily_summaries(dog_id)   # cache only; recomputed on the next read
        return len(ids)

    async def load(self, seed_name: str, admin: Actor, dog_id: str,
                   device_id: Optional[str] = None, date: Optional[str] = None) -> dict:
        self._check_dog(admin, dog_id)
        path = seeds_dir() / Path(seed_name).name
        if not path.exists():
            raise HTTPException(status_code=404, detail={"code": "seed_not_found", "seed": seed_name})
        try:
            seed = json.loads(path.read_text())
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": str(e)})
        if not isinstance(seed.get("events"), list) or not seed["events"]:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": "events[] required"})
        try:
            h, m = (seed.get("day_start") or "07:00").split(":")
            day_start_s = int(h) * 3600 + int(m) * 60
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "bad_seed", "reason": "bad day_start"})
        device = self._resolve_device(dog_id, device_id, seed.get("device_id"))
        tz = settings_service.get_timezone(dog_id)
        date = date or datetime.now(timezone.utc).astimezone(tz).date().isoformat()
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "bad_date"})

        await self._stop()   # one replay at a time: loading here stops any other dog's replay
        self.seed_name, self.seed = path.name, seed
        self.dog_id, self.device_id, self.user_id = dog_id, device, admin.user_id
        self.tz, self.day_start_s, self.date = tz, day_start_s, date
        entries = sorted(seed["events"], key=lambda e: float(e.get("offset_s", 0)))
        entries, media_missing = self._link_media(entries)
        if media_missing:
            logger.warning(f"[REPLAY] {self.seed_name}: media not in seeds/media: {[m['file'] for m in media_missing]}")

        landed_date = (day - timedelta(days=1)).date().isoformat() if seed.get("land_previous_day") else None
        await self._purge(dog_id, [date] + ([landed_date] if landed_date else []))
        landed = 0
        if landed_date:
            # yesterday's recap: the whole seed, silently (no pushes, no WS frames, no agent lines)
            batch = [e["event"] for e in self._materialise(entries, landed_date)]
            res = await event_store.ingest_events(batch, device_id=device, user_id=admin.user_id,
                                                  source="replay", notify=False, forward=False)
            landed = len(res["accepted"])
            await self._send_apps({"type": "replay_landed", "dog_id": dog_id, "date": landed_date, "landed": landed})

        self.events = self._materialise(entries, date)
        self.cursor = 0
        self.emitted = 0
        self.started_at = None
        self._anchor(0.0)
        self.status = "loaded"
        ignored = sorted((seed.get("settings") or {}).keys())
        logger.info(f"[REPLAY] loaded {self.seed_name}: {len(self.events)} events for {dog_id} on {date}"
                    + (f", landed {landed} on {landed_date}" if landed_date else "")
                    + (f", seed settings ignored: {ignored}" if ignored else ""))
        return {**self.state(), "landed": landed, "landed_date": landed_date, "settings_ignored": ignored,
                "media_missing": media_missing}

    async def start(self, speed: Optional[float] = None, dog_id: Optional[str] = None) -> dict:
        self._require_target(dog_id)
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

    async def pause(self, dog_id: Optional[str] = None) -> dict:
        self._require_target(dog_id)
        self._anchor()
        self._running = False
        if self.status == "running":
            self.status = "paused"
        self._wake.set()
        return self.state()

    async def set_speed(self, speed: float, dog_id: Optional[str] = None) -> dict:
        self._require_target(dog_id)
        self._anchor()
        self.speed = float(speed)
        self._wake.set()
        return self.state()

    async def jump(self, offset_s: Optional[float] = None, to: Optional[str] = None,
                   dog_id: Optional[str] = None) -> dict:
        """landed = events this call wrote (forward); removed = replay events it took back (backward)."""
        self._require_target(dog_id)
        if to:
            h, m = to.split(":")
            offset_s = int(h) * 3600 + int(m) * 60 - self.day_start_s
        if offset_s is None:
            raise HTTPException(status_code=422, detail={"code": "jump_target_required"})
        offset_s = max(0.0, float(offset_s))
        catch_up: list[dict] = []
        landed = removed = 0
        was_running = self._running
        self._running = False
        self._anchor()
        k = next((i for i, e in enumerate(self.events) if e["offset_s"] > offset_s), len(self.events))
        if k < self.cursor:
            # backward: drop everything already emitted at/after the new cursor
            ids = [e["event_id"] for e in self.events[k:self.cursor]]
            removed = delete_events_by_ids(ids)
            delete_activity_events_by_ids(ids)
            delete_notify_log_by_event_ids(ids)
            delete_treat_ledger_by_event_ids(ids)
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
            landed = len(res["accepted"])
            self.emitted += landed
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
        logger.info(f"[REPLAY] jump {self.dog_id} -> {self.virtual_time().isoformat()} landed={landed} removed={removed}")
        return {**self.state(), "landed": landed, "removed": removed, "catch_up": catch_up}

    async def _stop(self) -> None:
        """Stop the loop and unload (no data deleted)."""
        self._running = False
        self._wake.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.status = "idle"
        self.cursor = 0
        self.emitted = 0
        self.started_at = None
        self._anchor(0.0)

    async def reset_dog(self, dog_id: str, admin: Actor) -> dict:
        """Delete this dog's replay-made data, even when nothing is loaded for it. Stops the replay
        only if it is this dog's; another dog's replay keeps running (named in busy_with)."""
        self._check_dog(admin, dog_id)
        if self.dog_id == dog_id and self.status != "idle":
            await self._stop()
        removed = await self._purge(dog_id)
        await self._send_apps({"type": "replay_reset", "dog_id": dog_id}, admin.user_id)
        logger.info(f"[REPLAY] reset: removed {removed} events for {dog_id}")
        return {**self.state_for(dog_id), "removed": removed}

    async def reset(self) -> dict:
        """Stop and delete the loaded dog's replay data (internal / tests)."""
        dog_id = self.dog_id
        await self._stop()
        removed = await self._purge(dog_id) if dog_id else 0
        if dog_id:
            await self._send_apps({"type": "replay_reset", "dog_id": dog_id})
        return {**self.state(), "removed": removed}

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
        elif cmd in ("sleep", "wake"):   # 2026-11-19
            events.append({"type": "mode_change", "ts": now.isoformat(),
                           "data": {"mode": "sleep" if cmd == "sleep" else "idle", "source": src}})
            events.append({"type": "status", "ts": now.isoformat(),
                           "data": {"battery": 80, "mode": "sleep" if cmd == "sleep" else "idle",
                                    "treats_remaining": 8}})
        elif cmd == "go_to":
            events.append({"type": "status", "ts": now.isoformat(),
                           "data": {"battery": 80, "mode": "mission", "location": data.get("target")}})
        for e in events:
            e["event_id"] = f"relay:{uuid.uuid4()}"
            e["dog_id"] = dog_id
        if events:
            await event_store.ingest_events(events, device_id=self.device_id or get_settings().demo_device_id,
                                            user_id=self.user_id, source="relay")

    async def _send_apps(self, msg: dict, user_id: Optional[str] = None) -> None:
        try:
            from app.connection_manager import get_connection_manager
            if user_id or self.user_id:
                await get_connection_manager().send_to_user_apps(user_id or self.user_id, msg)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[REPLAY] app notify failed: {e}")


_replayer: Optional[Replayer] = None


def get_replayer() -> Replayer:
    global _replayer
    if _replayer is None:
        _replayer = Replayer()
    return _replayer
