"""Awaitable robot command results (amendment 2026-11-06, item 1).

The robot answers every relay command with
`{"type": "command_result", "command_id", "ok", "result", "error"}` over its WebSocket.
`robot_control.dispatch` registers the command_id before sending; the WS handler calls
`resolve`; HTTP callers can wait (`wait_s`) or poll the stored result (kept 10 minutes).
In-process only (single relay worker).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESULT_TTL_S = 600
_pending: dict[str, asyncio.Future] = {}
_results: dict[str, dict] = {}
_meta: dict[str, dict] = {}


def register(command_id: str, *, dog_id: Optional[str] = None, device_id: Optional[str] = None,
             command: Optional[str] = None) -> None:
    _meta[command_id] = {"dog_id": dog_id, "device_id": device_id, "command": command, "sent_at": time.time()}
    try:
        loop = asyncio.get_running_loop()
        if command_id not in _pending:
            _pending[command_id] = loop.create_future()
    except RuntimeError:  # pragma: no cover
        pass
    _gc()


def resolve(command_id: str, payload: dict) -> Optional[dict]:
    """Store a result and wake any waiter. Returns the meta (dog_id/device_id/command) if known."""
    result = {
        "command_id": command_id,
        "ok": bool(payload.get("ok", True)),
        "result": payload.get("result"),
        "error": payload.get("error"),
        "timestamp": payload.get("timestamp"),
        "received_at": time.time(),
    }
    meta = _meta.get(command_id) or {}
    result.update({k: meta.get(k) for k in ("dog_id", "device_id", "command")})
    _results[command_id] = result
    fut = _pending.pop(command_id, None)
    if fut and not fut.done():
        fut.set_result(result)
    return meta or None


async def wait(command_id: str, timeout_s: float) -> Optional[dict]:
    if command_id in _results:
        return _results[command_id]
    fut = _pending.get(command_id)
    if fut is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


def get(command_id: str) -> Optional[dict]:
    return _results.get(command_id)


def _gc() -> None:
    cutoff = time.time() - RESULT_TTL_S
    for cid in [c for c, r in _results.items() if r.get("received_at", 0) < cutoff]:
        _results.pop(cid, None)
        _meta.pop(cid, None)
    for cid in [c for c, m in _meta.items() if m.get("sent_at", 0) < cutoff and c not in _results]:
        _meta.pop(cid, None)
        fut = _pending.pop(cid, None)
        if fut and not fut.done():
            fut.cancel()
