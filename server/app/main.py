"""
Main FastAPI Application
GPS Tracking Server
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import init_db
from app.tcp_server import TCPServer
from app.api import devices, locations
from app import dashboard

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global TCP server instance
tcp_server = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global tcp_server
    
    # Startup
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    
    # Initialize database
    logger.info("Initializing database...")
    init_db()
    
    # Start TCP server in background
    logger.info(f"Starting TCP server on port {settings.TCP_PORT}...")
    tcp_server = TCPServer(host=settings.HOST, port=settings.TCP_PORT)
    app.state.tcp_server = tcp_server
    
    # Run TCP server in background task
    tcp_task = asyncio.create_task(tcp_server.start())
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    tcp_task.cancel()
    try:
        await tcp_task
    except asyncio.CancelledError:
        pass


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="GPS Tracking Server for managing GPS tracker devices",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(devices.router, prefix="/api/devices", tags=["devices"])
app.include_router(locations.router, prefix="/api/locations", tags=["locations"])
app.include_router(dashboard.router, tags=["dashboard"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "tcp_port": settings.TCP_PORT,
        "http_port": settings.HTTP_PORT
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "tcp_connections": len(tcp_server.connections) if tcp_server else 0,
        "active_devices": len(tcp_server.device_connections) if tcp_server else 0
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.HTTP_PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )
