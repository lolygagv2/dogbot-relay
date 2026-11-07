"""WIM-Z MCP server: streamable HTTP at /mcp + OAuth 2.1 authorization server.

Separate process (run_mcp.py, 127.0.0.1:8001) behind nginx on api.wimzai.com. The MCP SDK
builds /authorize, /token, /register, /revoke, the well-known documents and the bearer
middleware from `WimzOAuthProvider`; this module adds the login/consent pages and the
/register override on a parent Starlette app, and runs the session manager lifespan.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.config import get_settings
from app.mcp_server import login_routes, oauth_db
from app.mcp_server.authz import ALL_SCOPES
from app.mcp_server.oauth_provider import WimzOAuthProvider, issuer_url, resource_url
from app.mcp_server.tools import register_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("wimz.mcp")

settings = get_settings()
provider = WimzOAuthProvider()

INSTRUCTIONS = """WIM-Z is a home robot that keeps a dog company and coaches it while the owner is away.
Use get_dogs first to learn the dog_id. Read tools return the owner's roll-ups, events and robot
status; quote only numbers that appear in those results. Control tools are limited by the owner's
consent scopes and by guards on the robot side: treats are capped per day, quiet hours can only be
turned on or widened, and live video can never be turned on from here (with the photos / camera
permissions you may fetch an event's snapshot or ask for a single new snapshot). When a tool returns
refused=true, tell the user the reason plainly. To start or stop Silent Guardian (bark watch) now, use set_silent_guardian. To run Silent Guardian, Coach mode or a training mission at a set time, use schedule_mission (it schedules immediately; there is no approval step), get_schedule to see what is scheduled and when it next fires, and cancel_mission to delete one. Only say something is scheduled on the robot when confirmed_by_robot is true. For supervised movement: take a photo, decide one move, take another photo. Turn to face away before driving away from something. Stop and explain after 8 move cycles, or after a full 360-degree search finds nothing."""

ALLOWED_ORIGINS = [
    "https://claude.ai", "https://claude.com", "https://*.claude.ai", "https://*.claude.com",
    "https://chatgpt.com", "https://chat.openai.com", "https://*.openai.com",
    "https://www.perplexity.ai", "https://perplexity.ai", "https://*.perplexity.ai",
    "https://grok.com", "https://x.ai", "https://*.x.ai", "https://*.grok.com",
    "http://127.0.0.1:*", "http://localhost:*",
]

mcp = MCPServer(
    name="WIM-Z",
    title="WIM-Z dog robot",
    instructions=INSTRUCTIONS,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(issuer_url()),
        resource_server_url=AnyHttpUrl(resource_url()),
        service_documentation_url=AnyHttpUrl(f"{settings.public_base_url.rstrip('/')}/docs"),
        required_scopes=["read"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=list(ALL_SCOPES), default_scopes=list(ALL_SCOPES),
        ),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,  # provider.load_access_token applies a lenient resource check
    ),
)
register_tools(mcp)

allowed_hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=ALLOWED_ORIGINS,
    ),
)


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            logger.info(f"[MCP] retention sweep: {oauth_db.sweep()}")
        except Exception as e:  # pragma: no cover
            logger.warning(f"[MCP] sweep failed: {e}")


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    oauth_db.init_oauth_tables()
    logger.info(f"[MCP] issuer={issuer_url()} resource={resource_url()} hosts={allowed_hosts} "
                f"relay={settings.relay_base_url} internal_token={'set' if settings.internal_api_token else 'MISSING'}")
    task = asyncio.create_task(_sweep_loop())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        task.cancel()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "wimz-mcp", "issuer": issuer_url(), "resource": resource_url(),
                         "tools": len(mcp._tool_manager.list_tools()) if hasattr(mcp, "_tool_manager") else None})


app = Starlette(
    routes=[
        Route("/mcp/health", health, methods=["GET"]),
        *login_routes.routes,
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
)
