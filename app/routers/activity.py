"""Activity event log endpoints (Phase 3 / A3)."""
import base64
import logging
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.connection_manager import get_connection_manager
from app.database import clear_activity_events, query_activity_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["Activity"])


class ClearActivityRequest(BaseModel):
    """Body for POST /api/activity/clear (L-SYNC / Chain SG).

    mode='events' dismisses specific rows (requires event_ids).
    mode='all' clears the user's feed, optionally scoped to a dog and/or up to a
    timestamp (dog_id / before_ts). event_ids is ignored when mode='all'.
    """
    mode: Literal["events", "all"]
    event_ids: Optional[list[str]] = Field(default=None, description="Rows to clear when mode='events'")
    dog_id: Optional[str] = Field(default=None, description="Scope clear-all to one dog")
    before_ts: Optional[str] = Field(default=None, description="Clear-all only events at/older than this ISO ts")


def _encode_cursor(timestamp: str, event_id: str) -> str:
    raw = f"{timestamp}|{event_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    # Re-pad base64
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    if "|" not in raw:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    timestamp, event_id = raw.split("|", 1)
    return timestamp, event_id


def _to_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "dog_id": row["dog_id"],
        "type": row["type"],
        "timestamp": row["timestamp"],
        "payload": row["payload"],
    }


@router.get("")
async def get_activity(
    current_user: Annotated[dict, Depends(get_current_user)],
    dog_id: Optional[str] = Query(None, description="Filter to a single dog; omit for all dogs"),
    since: Optional[str] = Query(None, description="Lower bound on timestamp (ISO 8601)"),
    limit: int = Query(100, ge=1, le=500, description="Max events to return; default 100, cap 500"),
    cursor: Optional[str] = Query(None, description="Opaque cursor from a prior response"),
):
    """Fetch activity events for the authenticated user.

    Sorted by timestamp DESC. When more results remain, returns a `next_cursor`
    that can be passed back to retrieve the next page.
    """
    user_id = current_user["user_id"]
    cursor_ts: Optional[str] = None
    cursor_id: Optional[str] = None
    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)

    rows = query_activity_events(
        user_id=user_id,
        dog_id=dog_id,
        since=since,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
        limit=limit + 1,  # peek for next page
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor: Optional[str] = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["timestamp"], last["id"])

    return {
        "events": [_to_response(r) for r in page],
        "next_cursor": next_cursor,
    }


@router.post("/clear")
async def clear_activity(
    body: ClearActivityRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Durably clear Silent Guardian / activity events for the authenticated user.

    L-SYNC / Chain SG. Two steps:
      1. Persist the clear at the relay (soft-clear via cleared_at) so the events
         do NOT reappear on the next GET /api/activity. This alone fixes the bug.
      2. Best-effort round-trip: forward the clear over WS to the owning robot(s)
         so the Edge (system of record) can mark its own rows. Never blocks the
         response; an offline robot reconciles on its next sync.
    """
    user_id = current_user["user_id"]

    if body.mode == "events":
        if not body.event_ids:
            raise HTTPException(status_code=400, detail="event_ids required when mode='events'")
        result = clear_activity_events(user_id, event_ids=body.event_ids)
    else:  # mode == "all"
        result = clear_activity_events(user_id, dog_id=body.dog_id, before_ts=body.before_ts)

    cleared_at = datetime.now(timezone.utc).isoformat()

    # Step 2: round-trip to the owning robot(s). Only devices whose rows were
    # actually cleared, and only ones this user owns. Failures are swallowed.
    manager = get_connection_manager()
    forwarded_to: list[str] = []
    for device_id in result["device_ids"]:
        if manager.get_device_owner(device_id) != user_id:
            continue
        try:
            delivered = await manager.send_to_robot(device_id, {
                "type": "activity_clear",
                "event_ids": result["event_ids"],
                "cleared_at": cleared_at,
                "mode": body.mode,
                "dog_id": body.dog_id,
                "before_ts": body.before_ts,
            })
            if delivered:
                forwarded_to.append(device_id)
        except Exception as e:  # pragma: no cover - defensive; forward is best-effort
            logger.warning(f"[ACTIVITY-CLEAR] robot forward to {device_id} failed: {e}")

    logger.info(
        f"[ACTIVITY-CLEAR] user={user_id} mode={body.mode} "
        f"cleared={result['cleared']} forwarded_to={forwarded_to}"
    )
    return {
        "cleared": result["cleared"],
        "event_ids": result["event_ids"],
        "forwarded_to": forwarded_to,
    }
