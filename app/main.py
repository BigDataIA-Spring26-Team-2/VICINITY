"""Vicinity API — FastAPI application entry point.

Lifespan:
    Startup: run migrations, warm Redis, log ready state.
    Shutdown: tear down Redis singleton.

Routers:
    /healthz, /ping     — infrastructure health checks
    /auth/*             — registration, login, session resume
    /chat/*             — SSE streaming agent endpoint
    /users/*            — authenticated user data reads
    /listings/*         — listing search, detail, compare, scorecard
    /map/*              — GeoJSON endpoints for map layers
    /scorecards/*       — route corridor safety time series
    /safety/*           — crime and complaint data

Usage:
    uvicorn app.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import run_migrations
from app.core.cache import RedisCache
from app.routers import health, auth, chat, users

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    # Startup
    run_migrations()
    RedisCache.get_instance()
    logger.info("app_ready")

    yield

    # Shutdown
    RedisCache.reset()
    logger.info("app_shutdown")


app = FastAPI(
    title="Vicinity",
    description="Boston housing intelligence API",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allows the React dev server (localhost:3000/5173) to call the API.
# In production, restrict origins to the actual frontend domain.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",    # Create React App default
        "http://localhost:5173",    # Vite default
        "http://localhost:8501",    # Streamlit (legacy)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers — existing
# ---------------------------------------------------------------------------
app.include_router(health.router)
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])
app.include_router(users.router, prefix="/users", tags=["users"])

# ---------------------------------------------------------------------------
# Routers — frontend dashboard, map, charts
# ---------------------------------------------------------------------------
from app.routers import listings, map, scorecards, safety

app.include_router(listings.router, prefix="/listings", tags=["listings"])
app.include_router(map.router, prefix="/map", tags=["map"])
app.include_router(scorecards.router, prefix="/scorecards", tags=["scorecards"])
app.include_router(safety.router, prefix="/safety", tags=["safety"])