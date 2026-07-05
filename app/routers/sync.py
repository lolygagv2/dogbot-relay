"""Incremental sync ingest — Chain STORE plumbing (scaffold).

The relay is the RECEIVER of opt-in, watermark-based sync from the edge
(WIMZ_Data_Architecture_Spec.md section 9). The edge pushes rows newer than its
high-water mark; the relay upserts them idempotently by primary key and advances
`sync_state`.

STATUS: SCAFFOLD. The endpoint, validation, and watermark advance are real. The
per-table landing upsert is intentionally NOT implemented: the edge tables
(`event`, `training_attempt`, `outcome_snapshot`, `media_asset`, `dispense_log`,
`session`, `dog`) are defined authoritatively in WIMZ_Data_Architecture_Spec.md,
and their landing copies must be created from that spec verbatim as part of
Workstream A — not invented here. Until then, ingest validates + records the
watermark so the pipe is wired end to end without committing unspec'd schema.
"""
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.database import advance_sync_state, get_sync_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["Sync"])

# Tables the edge may sync up, gated by consent (spec section 9). This is exactly
# the set carrying a `synced` flag in WIMZ_Data_Architecture_Spec.md section 4 —
# do not extend without bumping the spec.
SPEC_SYNCABLE_TABLES = frozenset({
    "event",
    "training_attempt",
    "dispense_log",
    "media_asset",
    "outcome_snapshot",
    "session",
    "dog",
})


class SyncBatch(BaseModel):
    """One table's worth of rows newer than the edge's high-water mark."""
    table_name: str = Field(description="Spec table name; must be in SPEC_SYNCABLE_TABLES")
    rows: list[dict[str, Any]] = Field(description="Rows keyed by their UUIDv7 primary key")


@router.post("/ingest")
async def ingest(
    batch: SyncBatch,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Receive an incremental-sync batch for one spec table.

    Real: validates the table against the spec allowlist and advances the
    `sync_state` high-water mark to the newest row timestamp.

    Stubbed (Workstream A): the idempotent upsert-by-primary-key into the landing
    copy of the table. Those landing tables must be created verbatim from
    WIMZ_Data_Architecture_Spec.md; this scaffold does not invent them.
    """
    if batch.table_name not in SPEC_SYNCABLE_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"'{batch.table_name}' is not a syncable spec table",
        )

    # Newest row timestamp in this batch becomes the candidate high-water mark.
    # Spec uses created_at/updated_at (epoch ms) as the watermark field.
    def _row_ts(row: dict) -> int:
        val = row.get("updated_at") or row.get("created_at") or 0
        return int(val) if isinstance(val, (int, float)) else 0

    high_water = max((_row_ts(r) for r in batch.rows), default=0)

    # TODO(Workstream A): upsert batch.rows into the landing copy of
    # `batch.table_name` (idempotent ON CONFLICT(<pk>) DO UPDATE), applying
    # edge-authoritative-for-machine / app-authoritative-for-human conflict rules
    # (spec section 2) and PII stripping per consent (spec section 9). The landing
    # schema comes from WIMZ_Data_Architecture_Spec.md — create it there, not here.
    accepted = 0  # will become the upserted row count once landing tables exist

    if high_water > 0:
        advance_sync_state(batch.table_name, high_water)

    logger.info(
        "[SYNC-INGEST] table=%s rows=%d high_water=%d user=%s (landing upsert stubbed)",
        batch.table_name, len(batch.rows), high_water, current_user["user_id"],
    )
    return {
        "table_name": batch.table_name,
        "received": len(batch.rows),
        "accepted": accepted,
        "high_water": high_water,
        "note": "landing persistence stubbed pending Workstream A spec tables",
    }


@router.get("/state")
async def sync_status(
    current_user: Annotated[dict, Depends(get_current_user)],
    table_name: Optional[str] = None,
):
    """Read the incremental-sync watermark(s). Observability for the pipe."""
    if table_name is not None:
        state = get_sync_state(table_name)
        return {"states": [state] if state else []}
    return {"states": [s for s in (get_sync_state(t) for t in sorted(SPEC_SYNCABLE_TABLES)) if s]}
