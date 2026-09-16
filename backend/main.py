"""Braintuitive FastAPI application entrypoint.

Run locally:
    cd backend
    uvicorn main:app --reload

Startup wires up SQLite, MongoDB (optional) and the APScheduler jobs; shutdown
tears them back down. Routers are mounted defensively so that the app still
boots while Phase 4 route modules are being written.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import configure_logging, settings
from db.database import (
    check_mongo_connection,
    check_sql_connection,
    close_mongo_connection,
    connect_to_mongo,
    init_db,
)

configure_logging()
logger = logging.getLogger("braintuitive")

# Populated during startup when SCHEDULER_ENABLED.
scheduler: Any = None


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of every external resource."""
    global scheduler

    logger.info(
        "Starting %s v%s [%s]", settings.APP_NAME, settings.APP_VERSION, settings.ENVIRONMENT
    )

    for warning in settings.warn_on_missing_secrets():
        logger.warning(warning)

    # --- SQLite -----------------------------------------------------------
    init_db()

    # --- MongoDB (optional) ----------------------------------------------
    await connect_to_mongo()

    # --- APScheduler ------------------------------------------------------
    if settings.SCHEDULER_ENABLED:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            scheduler = AsyncIOScheduler(timezone=settings.SCHEDULER_TIMEZONE)
            scheduler.start()
            logger.info("APScheduler started (tz=%s)", settings.SCHEDULER_TIMEZONE)

            from services.scheduler_jobs import register_jobs

            register_jobs(scheduler)
        except Exception as exc:
            logger.error("Scheduler failed to start (continuing without it): %s", exc)
            scheduler = None

    logger.info("Startup complete - docs at http://%s:%d/docs", settings.HOST, settings.PORT)

    yield

    # --- Shutdown ---------------------------------------------------------
    logger.info("Shutting down...")
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
    await close_mongo_connection()
    logger.info("Shutdown complete.")


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "AI-powered homework generation. Personalised, curriculum-aligned quizzes "
        "with immediate Khan-Academy-style feedback and genuine milestone celebration."
    ),
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback, return a clean 500 without leaking internals."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    detail = str(exc) if settings.DEBUG else "Internal server error"
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": detail, "path": request.url.path},
    )


# --------------------------------------------------------------------------- #
# System routes
# --------------------------------------------------------------------------- #


@app.get("/", tags=["system"])
async def root() -> dict[str, Any]:
    """Service banner."""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    """Liveness and dependency check for monitoring."""
    sqlite_ok = check_sql_connection()
    mongo_ok = await check_mongo_connection()

    return {
        "status": "healthy" if sqlite_ok else "degraded",
        "version": settings.APP_VERSION,
        # The deployed commit, so a browser can answer "did my change ship?".
        # Render sets this; empty anywhere else.
        "commit": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7],
        "dependencies": {
            "sqlite": "up" if sqlite_ok else "down",
            "mongodb": "up" if mongo_ok else "not_configured",
            "pinecone": "configured" if settings.pinecone_configured else "not_configured",
            "nebius": "configured" if settings.nebius_configured else "not_configured",
            "scheduler": "running" if scheduler is not None else "stopped",
        },
    }


# --------------------------------------------------------------------------- #
# Feature routers (Phase 4)
# --------------------------------------------------------------------------- #


def _mount_routers() -> None:
    """Mount route modules that exist, skipping the ones not yet written.

    Keeps the app bootable throughout Phase 4 instead of failing at import on
    the first missing module.
    """
    modules = ("auth", "curriculum", "quiz", "progress", "gamification")
    for name in modules:
        try:
            module = __import__(f"api.routes.{name}", fromlist=["router"])
            app.include_router(module.router, prefix=settings.API_V1_PREFIX)
            logger.info("Mounted router: %s", name)
        except ModuleNotFoundError:
            logger.debug("Router not yet implemented, skipping: %s", name)
        except Exception as exc:
            # A router that exists but fails to mount is a bug, not a missing
            # feature. Outside development it must stop the boot rather than
            # leave the endpoint quietly absent.
            logger.error("Failed to mount router %s: %s", name, exc)
            if settings.ENVIRONMENT != "development":
                raise


_mount_routers()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
