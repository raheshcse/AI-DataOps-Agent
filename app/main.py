"""Minimal FastAPI application (Phase 1: health and readiness only)."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import __version__
from app.api.simulation import router as simulation_router
from app.api.v1 import create_api_app
from app.config import get_settings
from app.core.logging import RedactionFilter, configure_logging
from app.core.startup import validate_startup

logger = logging.getLogger("app.request")
logger.addFilter(RedactionFilter())  # applies to records logged on this logger


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Phase 9: in production only, refuse to start on missing/unsafe configuration (names only in the error).
    validate_startup()
    # Logging setup must never stop the app from starting: /health has to stay up
    # even if configuration is invalid (/ready reports that).
    try:
        configure_logging(get_settings().log_level)
    except ValidationError:
        configure_logging("INFO")
    yield


app = FastAPI(
    title="AI DataOps Agent",
    version=__version__,
    description="AI DataOps Agent with VSL governance. Phase 1: foundation only.",
    lifespan=lifespan,
)


app.include_router(simulation_router)


def _build_services():
    from app.services.container import build_services

    return build_services()


# Phase 8: the versioned API is a mounted sub-application; services are built lazily on first use.
app.mount("/api/v1", create_api_app(_build_services))


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Log method, path and status only: never query strings, headers or bodies.
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.error("%s %s -> unhandled error", request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, __: Exception) -> JSONResponse:
    # Generic body: never leak exception details or configuration values.
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not touch configuration."""
    return {"status": "ok", "service": app.title, "version": __version__}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: configuration is valid and required values are present.

    Reports only whether each item is configured, never the values.
    """
    checks: dict[str, str] = {}
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Field names only; error details can echo the invalid input.
        invalid = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        checks["configuration"] = "invalid"
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": checks, "invalid_fields": invalid},
        )

    checks["configuration"] = "valid"
    checks["openai_api_key"] = "configured" if settings.openai_configured else "missing"
    is_ready = settings.openai_configured
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else "not_ready",
            "environment": settings.app_env,
            "version": __version__,
            "checks": checks,
        },
    )


if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run("app.main:app", host=cfg.api_host, port=cfg.api_port)
