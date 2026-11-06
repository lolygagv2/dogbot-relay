#!/usr/bin/env python3
"""Run the WIM-Z MCP server (OAuth 2.1 AS + /mcp) on 127.0.0.1:<MCP_PORT> (default 8001).

systemd unit: deploy/wimz-mcp.service. nginx routes /mcp, /authorize, /token, /register,
/revoke and /.well-known/oauth-* here (deploy/nginx-wimz-mcp.conf). Single worker.
"""
import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.mcp_server.server:app",
        host="127.0.0.1",
        port=settings.mcp_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
