"""Scopes, per-tool authorization and the audit wrapper for the MCP server.

Scope model (per-assistant tokens):
  read      summaries, events, trend, missions list, robot status (always granted)
  settings  quiet hours, music plan
  missions  draft + schedule missions
  physical  give a treat, send the robot somewhere, speak through it

Scopes are enforced per tool (not at the transport) so a read-only token can still
list and call read tools; a missing scope surfaces as an actionable ToolError.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Callable, Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp_server import oauth_db

logger = logging.getLogger(__name__)

ALL_SCOPES = ["read", "settings", "missions", "physical", "photos", "camera"]
SCOPE_LABELS = {
    "read": "Read summaries, events and robot status",
    "settings": "Change quiet hours, music and sensitivity",
    "missions": "Draft and schedule missions",
    "physical": "Give a treat, move the robot or speak through it",
    "photos": "See snapshots WIM-Z already took",
    "camera": "Ask WIM-Z to take a new snapshot (never live video)",
}
# Off by default on the consent screen even when requested (PHOTO ACCESS 2026-11-06).
OPT_IN_SCOPES = {"photos", "camera"}
TOOL_SCOPES = {
    "get_dogs": "read", "get_day_summary": "read", "get_events": "read", "get_trend": "read",
    "get_missions": "read", "get_robot_status": "read",
    "set_quiet_hours": "settings", "set_music": "settings",
    "draft_mission": "missions", "schedule_mission": "missions",
    "give_treat": "physical", "go_to": "physical", "move": "physical", "speak": "physical",
    "get_event_photo": "photos", "take_photo": "camera",
}


def normalize_scopes(scopes: list[str]) -> list[str]:
    out = ["read"]
    for sc in scopes:
        if sc in ALL_SCOPES and sc not in out:
            out.append(sc)
    return out


def current_token() -> AccessToken:
    token = get_access_token()
    if token is None:
        raise ToolError("Not authenticated with WIM-Z.")
    return token


def require_scope(tool_name: str) -> AccessToken:
    token = current_token()
    needed = TOOL_SCOPES.get(tool_name, "read")
    if needed not in token.scopes and needed != "read":
        label = SCOPE_LABELS.get(needed, needed)
        raise ToolError(
            f"This assistant was not granted the '{needed}' permission for WIM-Z "
            f"({label}). Re-connect WIM-Z and tick '{label}' on the consent screen."
        )
    return token


def actor_label(token: Optional[AccessToken]) -> str:
    return f"connector:{token.client_id}" if token else "connector:unknown"


def _summarize(result: Any) -> str:
    try:
        if isinstance(result, dict):
            keys = ", ".join(list(result.keys())[:12])
            flags = []
            for k in ("ok", "refused", "reason"):
                if k in result:
                    flags.append(f"{k}={result[k]}")
            return (" ".join(flags) + " | keys: " + keys)[:512]
        return str(result)[:512]
    except Exception:  # pragma: no cover
        return "?"


def audited(tool_name: str) -> Callable:
    """Wrap an async tool: records every call (ok / refused / denied_scope / error) in mcp_audit."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.monotonic()
            token = get_access_token()
            outcome, summary, relay_status = "ok", None, None
            try:
                result = await fn(*args, **kwargs)
                if isinstance(result, dict) and result.get("refused"):
                    outcome = "refused"
                    relay_status = result.get("_status")
                    result.pop("_status", None)
                elif isinstance(result, list) and any(isinstance(b, str) and '"refused": true' in b for b in result):
                    outcome = "refused"
                summary = _summarize(result) if not isinstance(result, list) else f"{len(result)} content block(s)"
                return result
            except ToolError as e:
                outcome = "denied_scope" if "permission" in str(e) else "error"
                summary = str(e)[:512]
                raise
            except Exception as e:
                outcome = "error"
                summary = f"{type(e).__name__}: {e}"[:512]
                logger.exception(f"[MCP] tool {tool_name} failed")
                raise ToolError(f"{tool_name} failed: {type(e).__name__}") from e
            finally:
                dur = int((time.monotonic() - t0) * 1000)
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, functools.partial(
                        oauth_db.insert_audit,
                        actor_label(token), tool_name, outcome,
                        client_id=token.client_id if token else None,
                        client_name=(token.claims or {}).get("client_name") if token else None,
                        user_id=token.subject if token else None,
                        dog_id=kwargs.get("dog_id"),
                        scopes=token.scopes if token else None,
                        input_obj=kwargs, relay_status=relay_status,
                        result_summary=summary, duration_ms=dur,
                    ))
                except Exception as e:  # pragma: no cover
                    logger.warning(f"[MCP] audit write failed: {e}")
        return wrapper
    return deco
