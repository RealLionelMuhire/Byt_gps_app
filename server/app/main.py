"""
Main FastAPI Application
GPS Tracking Server
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import init_db
from app.tcp_server import TCPServer
from app.api import devices, locations, auth, commands, trips, webhooks
from app.api import ws as ws_module
from app.api import onboarding
from app import dashboard

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global TCP server instance
tcp_server = None


async def _trip_stale_checker():
    """Background task: end active trips either when the device stops
    sending entirely (stale last_update — the original check) or when it's
    still connected but has been stopped (speed below the owner's
    stop_speed_threshold_kmh) for stop_splits_trip_after_minutes. Without
    the second condition, a trip that starts on movement (see
    tcp_server.py's handle_location) would otherwise run forever as long as
    the device keeps pinging while parked.
    """
    from datetime import timedelta
    from app.core.database import SessionLocal
    from app.models.trip import Trip
    from app.models.device import Device
    from app.models.location import Location
    from app.services.trip_service import end_active_trips_for_device
    from app.api.trips import get_or_create_trip_settings

    while True:
        try:
            await asyncio.sleep(60)  # Run every 60 seconds
            now = datetime.utcnow()
            stale_cutoff = now - timedelta(seconds=settings.TRIP_AUTO_END_STALE_SECONDS)
            db = SessionLocal()
            try:
                active_trips = db.query(Trip).filter(Trip.end_time.is_(None)).all()
                for trip in active_trips:
                    device = db.query(Device).filter(Device.id == trip.device_id).first()
                    if not device:
                        continue

                    is_stale = (
                        device.last_update < stale_cutoff
                        if device.last_update is not None
                        else (device.last_connect is not None and device.last_connect < stale_cutoff)
                    )

                    reason = None
                    if is_stale:
                        reason = f"no update for {settings.TRIP_AUTO_END_STALE_SECONDS}s"
                    else:
                        # Still connected — check whether it's been stopped
                        # long enough (per the owner's own trip settings) to
                        # count as the trip having ended anyway.
                        trip_settings = get_or_create_trip_settings(trip.user_id, db)
                        last_moving = (
                            db.query(Location)
                            .filter(
                                Location.device_id == trip.device_id,
                                Location.gps_valid == True,
                                Location.timestamp >= trip.start_time,
                                Location.speed >= trip_settings.stop_speed_threshold_kmh,
                            )
                            .order_by(Location.timestamp.desc())
                            .first()
                        )
                        reference_time = last_moving.timestamp if last_moving else trip.start_time
                        stopped_for = timedelta(minutes=trip_settings.stop_splits_trip_after_minutes)
                        if now - reference_time >= stopped_for:
                            reason = f"stopped for {trip_settings.stop_splits_trip_after_minutes}m"

                    if reason:
                        ended = end_active_trips_for_device(trip.device_id, db)
                        if ended:
                            logger.info(
                                "Auto-ended %d trip(s) for device %s (%s)",
                                ended, trip.device_id, reason,
                            )
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Trip stale checker error: %s", e)


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

    # Attach the WebSocket connection manager so the TCP server can push
    # location updates to mobile clients in real time
    app.state.ws_manager = ws_module.manager
    tcp_server.ws_manager = ws_module.manager

    # Run TCP server in background task
    tcp_task = asyncio.create_task(tcp_server.start())

    # Run trip stale checker: end active trips when device stops sending
    trip_stale_task = asyncio.create_task(_trip_stale_checker())

    yield

    # Shutdown
    logger.info("Shutting down...")
    trip_stale_task.cancel()
    try:
        await trip_stale_task
    except asyncio.CancelledError:
        pass
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
    lifespan=lifespan,
    redirect_slashes=False  # Disable automatic trailing slash redirects for mobile compatibility
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router,       prefix="/api/auth",    tags=["authentication"])
app.include_router(devices.router,    prefix="/api/devices", tags=["devices"])
app.include_router(commands.router,   prefix="/api/devices", tags=["commands"])
app.include_router(locations.router,  prefix="/api/locations", tags=["locations"])
app.include_router(trips.router,      prefix="/api/trips",   tags=["trips"])
app.include_router(webhooks.router,   prefix="/api/webhooks", tags=["webhooks"])
app.include_router(ws_module.router,  tags=["websocket"])
app.include_router(dashboard.router,  tags=["dashboard"])

# Onboarding routes — device-level ones share /api/devices prefix
# so the mobile app URLs match without change
app.include_router(
    onboarding.router,
    prefix="/api",
    tags=["onboarding"],
)


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
