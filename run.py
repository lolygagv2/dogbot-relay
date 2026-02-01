#!/usr/bin/env python3
"""Run the WIM-Z Cloud Relay server."""
import os
import uvicorn

if __name__ == "__main__":
    # Enable reload only in debug/development mode
    reload = os.getenv("DEBUG", "false").lower() == "true"

    # WebSocket connection stability settings (P1: Build 34, updated Build 35)
    # Increased timeouts to handle app backgrounding/screen lock
    ws_ping_interval = int(os.getenv("WS_PING_INTERVAL", "30"))  # seconds
    ws_ping_timeout = int(os.getenv("WS_PING_TIMEOUT", "60"))    # seconds (increased from 20)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload,
        log_level="info",
        ws_ping_interval=ws_ping_interval,
        ws_ping_timeout=ws_ping_timeout,
    )
