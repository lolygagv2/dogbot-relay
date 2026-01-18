import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.connection_manager import get_connection_manager
from app.models import HealthResponse
from app.routers import auth, device, turn, websocket

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    description="Cloud relay server for WIM-Z robot communication",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
app.include_router(device.router)
app.include_router(turn.router)
app.include_router(websocket.router)


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Server health check endpoint."""
    return HealthResponse(status="ok")


@app.get("/stats", tags=["Health"])
async def get_stats():
    """Get server connection statistics."""
    manager = get_connection_manager()
    return manager.get_stats()


@app.on_event("startup")
async def startup_event():
    """Initialize resources on startup."""
    logger.info(f"Starting {settings.app_name}")
    logger.info(f"Debug mode: {settings.debug}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources on shutdown."""
    logger.info("Shutting down relay server")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug
    )
