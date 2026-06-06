"""Per-device event replay buffer.

Captures feed-worthy events from devices and replays them to apps on reconnect.
Buffered events are in-memory only (lost on restart), but seq counters are persisted
to SQLite so app watermarks remain valid across relay restarts.
"""

import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# Events worth buffering for offline replay
FEED_WORTHY_EVENTS = {
    "bark_detected",
    "dog_detected",
    "alert",
    "mission_progress",
    "mission_complete",
    "unknown_dog_detected",
    "activity_event",
    "treat_dispensed",
}

# Events excluded from seq assignment entirely
EXCLUDED_EVENTS = {"ping", "pong", "heartbeat"}

# WebRTC signaling — assigned no seq, not buffered
WEBRTC_PREFIXES = "webrtc_"

# Buffer limits
MAX_BUFFER_SIZE = 200
MAX_BUFFER_AGE_SECONDS = 24 * 60 * 60  # 24 hours

# Persist seq to DB every N increments (batch to reduce I/O)
SEQ_PERSIST_INTERVAL = 10


class EventBuffer:
    """Per-device ring buffer for feed-worthy events."""

    def __init__(self, device_id: str, initial_seq: int = 0):
        self._device_id = device_id
        self._seq: int = initial_seq
        self._last_persisted_seq: int = initial_seq
        self._buffer: deque[dict] = deque()
        self._latest_battery: Optional[dict] = None

    def append(self, event_type: str, message: dict) -> Optional[int]:
        """Ingest an event from a device.

        Returns the assigned seq number if the event was sequenced, or None for
        excluded/battery/webrtc events.
        """
        if not event_type:
            return None

        # Battery/heartbeat/ping: update battery snapshot, no seq
        if event_type in EXCLUDED_EVENTS or event_type == "battery":
            battery_val = message.get("battery") or message.get("level")
            if battery_val is not None:
                self._latest_battery = {
                    "battery": battery_val,
                    "ts_server": time.time(),
                }
            return None

        # WebRTC signaling: skip entirely
        if event_type.startswith(WEBRTC_PREFIXES):
            return None

        # Assign monotonic seq to all other events
        self._seq += 1
        seq = self._seq
        ts_server = time.time()

        # Only buffer feed-worthy events
        if event_type in FEED_WORTHY_EVENTS:
            self._buffer.append({
                "seq": seq,
                "ts_server": ts_server,
                "event": event_type,
                "payload": message,
            })
            self._evict()

        # Persist seq counter periodically
        if self._seq - self._last_persisted_seq >= SEQ_PERSIST_INTERVAL:
            self._persist_seq()

        return seq

    def _persist_seq(self) -> None:
        """Persist current seq to database."""
        try:
            from app.database import save_replay_seq
            save_replay_seq(self._device_id, self._seq)
            self._last_persisted_seq = self._seq
        except Exception as e:
            logger.error(f"[REPLAY] Failed to persist seq for {self._device_id}: {e}")

    def _evict(self) -> None:
        """Drop entries past MAX_BUFFER_SIZE count or MAX_BUFFER_AGE_SECONDS age."""
        # Count-based eviction
        while len(self._buffer) > MAX_BUFFER_SIZE:
            self._buffer.popleft()

        # Age-based eviction
        cutoff = time.time() - MAX_BUFFER_AGE_SECONDS
        while self._buffer and self._buffer[0]["ts_server"] < cutoff:
            self._buffer.popleft()

    def replay_since(self, last_seen_seq: int) -> list[dict]:
        """Return buffered entries with seq > last_seen_seq, oldest-first."""
        return [entry for entry in self._buffer if entry["seq"] > last_seen_seq]

    def get_latest_battery(self) -> Optional[dict]:
        """Return the latest battery snapshot, or None."""
        return self._latest_battery

    @property
    def current_seq(self) -> int:
        return self._seq

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)


class EventReplayManager:
    """Maps device_id → EventBuffer. Loads persisted seq counters on init."""

    def __init__(self):
        self._buffers: dict[str, EventBuffer] = {}
        self._persisted_seqs: dict[str, int] = {}
        # Load persisted seq counters from DB
        try:
            from app.database import get_replay_seqs
            self._persisted_seqs = get_replay_seqs()
            if self._persisted_seqs:
                logger.info(f"[REPLAY] Loaded {len(self._persisted_seqs)} persisted seq counters")
        except Exception as e:
            logger.warning(f"[REPLAY] Could not load persisted seqs (DB may not be initialized yet): {e}")

    def get_or_create(self, device_id: str) -> EventBuffer:
        if device_id not in self._buffers:
            # Seed at persisted + interval to avoid seq reuse after unclean restart
            persisted = self._persisted_seqs.get(device_id, 0)
            initial_seq = persisted + SEQ_PERSIST_INTERVAL
            self._buffers[device_id] = EventBuffer(device_id, initial_seq)
        return self._buffers[device_id]

    def get(self, device_id: str) -> Optional[EventBuffer]:
        return self._buffers.get(device_id)

    def stats(self) -> dict:
        """Per-device buffer stats for debug endpoint."""
        return {
            device_id: {
                "seq": buf.current_seq,
                "buffered": buf.buffered_count,
                "has_battery": buf.get_latest_battery() is not None,
            }
            for device_id, buf in self._buffers.items()
        }


# Module-level singleton
replay_manager = EventReplayManager()
