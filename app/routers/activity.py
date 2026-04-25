"""Activity event log endpoints (Phase 3 / A3)."""
import base64
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.database import query_activity_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["Activity"])


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
