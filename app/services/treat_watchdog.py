"""Tell the app when a treat it asked for never comes out.

The robot acks an app `dispense_treat` straight away but only sends the `treat_dispensed` event once
the treat is out. If the robot refuses silently (a local guard), the app hears nothing. Each app
treat command starts a timer; each `treat_dispensed` from that robot clears the oldest pending one.
If the timer runs out first, the owner's apps get an error frame (code TREAT_NOT_DISPENSED).

`treat_dispensed` carries no command id, so a treat from another source (agent, coaching) that lands
while an app treat is pending also clears it. That only hides a failure, never invents one.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

TREAT_CONFIRM_TIMEOUT_S = 40.0

SendToUser = Callable[[str, dict], Awaitable[int]]


class TreatWatchdog:
    def __init__(self, timeout_s: float = TREAT_CONFIRM_TIMEOUT_S):
        self.timeout_s = timeout_s
        # device_id -> pending entries, oldest first
        self._pending: dict[str, deque[dict[str, Any]]] = defaultdict(deque)

    def app_treat_sent(self, user_id: str, device_id: str, message: dict, send: SendToUser) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        entry: dict[str, Any] = {
            "user_id": user_id,
            "command_id": message.get("command_id") or data.get("command_id"),
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        entry["task"] = asyncio.create_task(self._expire(device_id, entry, send))
        self._pending[device_id].append(entry)

    def treat_dispensed(self, device_id: str) -> None:
        queue = self._pending.get(device_id)
        if not queue:
            return
        entry = queue.popleft()
        entry["task"].cancel()
        if not queue:
            self._pending.pop(device_id, None)

    def pending_count(self, device_id: str) -> int:
        return len(self._pending.get(device_id, ()))

    async def _expire(self, device_id: str, entry: dict, send: SendToUser) -> None:
        try:
            await asyncio.sleep(self.timeout_s)
        except asyncio.CancelledError:
            return
        queue = self._pending.get(device_id)
        if queue is None or entry not in queue:
            return
        queue.remove(entry)
        if not queue:
            self._pending.pop(device_id, None)
        logger.warning(
            f"[TREAT-TIMEOUT] {device_id}: app treat from {entry['user_id']} sent {entry['sent_at']} "
            f"not dispensed within {self.timeout_s:.0f}s"
        )
        payload = {
            "type": "error",
            "code": "TREAT_NOT_DISPENSED",
            "command": "dispense_treat",
            "device_id": device_id,
            "command_id": entry["command_id"],
            "sent_at": entry["sent_at"],
            "message": f"The robot didn't dispense the treat (no confirmation within {self.timeout_s:.0f} seconds).",
        }
        try:
            await send(entry["user_id"], payload)
        except Exception as e:  # never let a notify failure escape the task
            logger.error(f"[TREAT-TIMEOUT] notify failed for {entry['user_id']}: {e}")


_watchdog: Optional[TreatWatchdog] = None


def get_treat_watchdog() -> TreatWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = TreatWatchdog()
    return _watchdog
