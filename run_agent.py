#!/usr/bin/env python3
"""Run the WIM-Z voice agent on 127.0.0.1:<AGENT_PORT> (default 8002).

systemd unit: deploy/wimz-agent.service. nginx routes /agent/ here (deploy/nginx-wimz-mcp.conf).
"""
import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.voice_agent.app:app",
        host="127.0.0.1",
        port=settings.agent_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
