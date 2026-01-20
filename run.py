#!/usr/bin/env python3
"""Run the WIM-Z Cloud Relay server."""
import os
import uvicorn

if __name__ == "__main__":
    # Enable reload only in debug/development mode
    reload = os.getenv("DEBUG", "false").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload,
        log_level="info"
    )
