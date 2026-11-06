"""Async HTTP client for the relay's internal API (Dec 9 demo build, 2026-11-06).

Used by the MCP server (actor `connector:<client_id>`) and the voice agent (actor
`agent`). Both run as separate processes on the same box and never import the relay's
connection manager; every robot interaction goes through these endpoints so the
relay's guards (treat cap, quiet-hours widen-only, command allowlist) apply uniformly.

Relay refusals (guards, offline robot, unknown sound/mission, validation) are raised
as `RelayRefusal` so callers can turn them into plain tool results the assistant can
voice. Infrastructure failures raise `RelayError`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

REFUSAL_STATUSES = {403, 404, 409, 422, 429}


class RelayRefusal(Exception):
    def __init__(self, status: int, code: str, detail: dict):
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.detail = detail

    def as_result(self, **extra: Any) -> dict:
        out = {"ok": False, "refused": True, "reason": self.code}
        for k, v in self.detail.items():
            if k not in ("code",) and v is not None:
                out[k] = v
        out.update(extra)
        return out


class RelayError(Exception):
    pass


class RelayClient:
    def __init__(self, actor: str, user_id: str, timeout: float = 15.0):
        s = get_settings()
        if not s.internal_api_token:
            raise RelayError("INTERNAL_API_TOKEN is not configured")
        self.base = s.relay_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {s.internal_api_token}",
            "X-Actor": actor,
            "X-On-Behalf-Of": user_id,
        }
        self.timeout = timeout

    async def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, self.base + path, json=json, params=params, headers=self.headers)
        except httpx.HTTPError as e:
            raise RelayError(f"relay unreachable: {e}") from e
        if resp.status_code in (200, 201, 202):
            return resp.json() if resp.content else {}
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:200]}
        detail = body.get("detail", body) if isinstance(body, dict) else {"detail": body}
        if not isinstance(detail, dict):
            detail = {"detail": detail}
        code = str(detail.get("code") or detail.get("guard") or f"http_{resp.status_code}")
        if resp.status_code in REFUSAL_STATUSES:
            raise RelayRefusal(resp.status_code, code, detail)
        raise RelayError(f"relay {resp.status_code}: {code}")

    # ---- convenience wrappers ----

    async def get_summary(self, dog_id: str, date: str = "today", refresh: bool = False) -> dict:
        return await self.request("GET", f"/summary/{dog_id}/{date}", params={"refresh": "1"} if refresh else None)

    async def list_events(self, dog_id: str, since: Optional[str] = None, until: Optional[str] = None,
                          types: Optional[list[str]] = None, limit: int = 50, order: str = "desc") -> dict:
        params: dict[str, Any] = {"limit": limit, "order": order}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if types:
            params["type"] = ",".join(types)
        return await self.request("GET", f"/api/dogs/{dog_id}/events", params=params)

    async def get_status(self, dog_id: str) -> dict:
        return await self.request("GET", f"/api/dogs/{dog_id}/status")

    async def get_settings(self, dog_id: str) -> dict:
        return await self.request("GET", f"/api/dogs/{dog_id}/settings")

    async def get_setting(self, dog_id: str, key: str) -> dict:
        return await self.request("GET", f"/api/dogs/{dog_id}/settings/{key}")

    async def put_setting(self, dog_id: str, key: str, value: Any, expected_version: Optional[int] = None) -> dict:
        body: dict[str, Any] = {"value": value}
        if expected_version is not None:
            body["expected_version"] = expected_version
        return await self.request("PUT", f"/api/dogs/{dog_id}/settings/{key}", json=body)

    async def reset_setting(self, dog_id: str, key: str) -> dict:
        return await self.request("POST", f"/api/dogs/{dog_id}/settings/{key}/reset")

    async def command(self, dog_id: str, command: str, data: Optional[dict] = None,
                      device_id: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"command": command, "data": data or {}}
        if device_id:
            body["device_id"] = device_id
        return await self.request("POST", f"/api/dogs/{dog_id}/command", json=body)
