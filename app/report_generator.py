"""Session-report LLM layer — L-REPORT / Workstream B (Chain STORE).

Turns one session's structured rows into a short, owner-friendly natural-language
report via a single cheap Claude API call. No always-on model; per-call only.

Sequencing (per WIMZ_Implementation_Proposal_Queryable_Store_and_LLM_Reports.md):
  SCHEMA v0.2 session_report (done)  ->  ROBOT training_attempt assembly (Workstream A)
  ->  THIS layer (Workstream B)  ->  APP display (Workstream C)

STATUS: SCAFFOLD. The pipeline below is real and runnable end to end *given a
stats_json* — hashing, idempotency, the Claude call, and the store all work now.
The one piece that cannot run yet is `assemble_stats()`: it reads the edge's
`training_attempt` / `outcome_snapshot` / `event` rows, which the relay does not
have until Edge Workstream A produces them and the sync ingest lands them here
(see app/routers/sync.py). Until then `generate_session_report()` returns a
`blocked` result instead of calling the API.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings
from app.database import get_session_report, upsert_session_report

logger = logging.getLogger(__name__)

# System prompt: factual, owner-friendly, no invented numbers. Proposal B4.
_SYSTEM_PROMPT = (
    "You write a short, owner-friendly summary of a single dog-training session for "
    "the WIM-Z app. You are given a JSON object of already-computed statistics. "
    "Rules: use ONLY numbers present in the JSON — never invent or estimate figures. "
    "Be factual and warm, not salesy. 2-4 sentences. Refer to the dog by name if "
    "present. If a stat is absent, do not mention it."
)


def compute_input_hash(stats: dict) -> str:
    """sha256 of the canonical (sorted-key) stats JSON. Drives idempotency.

    Deterministic: the same session data always hashes to the same value, so a
    re-sync of an unchanged session reuses the stored report instead of calling
    the API again.
    """
    canonical = json.dumps(stats, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assemble_stats(session_id: str) -> Optional[dict]:
    """Deterministically build the stats_json for a session (NO LLM). Proposal B2.

    Pulls, for `session_id`: the training_attempt rows, the relevant
    outcome_snapshot deltas (this session vs the prior window), and a count of
    notable event rows (errors, pose_rejected). Returns a small structured dict,
    or None if the session has no attempts to summarize.

    TODO(Workstream A): these edge tables do not exist on the relay yet. Wire this
    up once the sync ingest (app/routers/sync.py) lands training_attempt /
    outcome_snapshot / event rows. Target shape:
        {
          "dog_id": "...", "dog_name": "Elsa",
          "session_minutes": 12,
          "attempts": [{"trick": "sit", "total": 6, "success": 5, "avg_latency_ms": 2100}],
          "deltas": [{"trick": "sit", "success_rate": 0.83, "prev_success_rate": 0.60}],
          "notable_events": {"pose_rejected": 1, "error": 0}
        }
    """
    logger.info(
        "[REPORT] assemble_stats(%s): blocked — edge training_attempt rows not yet "
        "synced to relay (Workstream A). No-op until sync ingest is live.",
        session_id,
    )
    return None


def _call_llm(stats: dict) -> str:
    """One Claude API call: stats_json in, summary_text out. Proposal B4.

    Uses claude-haiku-4-5 (cheap, short structured-summarization task). Imports
    the SDK lazily so this module loads even when `anthropic` isn't installed.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("anthropic_api_key not configured; cannot generate report")

    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency not installed in scaffold
        raise RuntimeError(
            "anthropic SDK not installed. Add `anthropic` to requirements.txt and "
            "pip install it before enabling report generation."
        ) from e

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.session_report_model,
        max_tokens=settings.session_report_max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                "Write the session summary from these stats:\n"
                + json.dumps(stats, sort_keys=True)
            ),
        }],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def generate_session_report(session_id: str, *, force: bool = False) -> dict:
    """Generate (or return the existing) session report for one session. Proposal B.

    Trigger: on session-close sync (preferred) or explicit app request — never on
    the robot hot path. Steps: assemble stats -> hash -> idempotency check ->
    one API call -> store.

    Returns a dict with a `status`:
      - "blocked":   no stats yet (Edge Workstream A not producing attempts).
      - "cached":    a report already existed for this exact input (no API call).
      - "generated": a fresh report was produced and stored.
    """
    settings = get_settings()

    # B2 — deterministic input assembly (no LLM). Blocked until Workstream A.
    stats = assemble_stats(session_id)
    if stats is None:
        return {"status": "blocked", "session_id": session_id, "report": None,
                "reason": "no training_attempt rows synced for this session yet"}

    input_hash = compute_input_hash(stats)

    # B3 — idempotency: same session + same input -> reuse, no second API call.
    if not force:
        existing = get_session_report(session_id, input_hash)
        if existing is not None:
            logger.info("[REPORT] cached hit for session=%s hash=%s", session_id, input_hash[:12])
            return {"status": "cached", "session_id": session_id, "report": existing}

    # B4 — generation (one Claude call).
    summary_text = _call_llm(stats)

    # B5 — store with provenance.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    report = upsert_session_report(
        report_id=str(uuid.uuid4()),
        session_id=session_id,
        dog_id=stats.get("dog_id", ""),
        generated_at=now_ms,
        model_id=settings.session_report_model,
        input_hash=input_hash,
        summary_text=summary_text,
        stats_json=stats,
    )
    logger.info("[REPORT] generated report for session=%s hash=%s", session_id, input_hash[:12])
    return {"status": "generated", "session_id": session_id, "report": report}
