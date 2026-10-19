"""One error shape for the whole /api/v1 surface: {"error": {"code", "message", "request_id", "details"?}}.

Messages are fixed, generic strings. Exception text, SQL, paths, tokens and stack traces never reach a response.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.persistence import StoreError

logger = logging.getLogger("app.api.v1")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None,
                 details: dict[str, Any] | None = None) -> None:
        self.status, self.code, self.message, self.headers, self.details = status, code, message, headers or {}, details
        super().__init__(code)


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def error_response(request: Request, status: int, code: str, message: str, *, headers: dict[str, str] | None = None,
                   details: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_of(request)}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


_STATUS_CODES = {400: "bad_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
                 409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type", 422: "validation_error",
                 429: "rate_limited"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message, headers=exc.headers, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and rule names only: never the submitted values (they may contain secrets).
        problems = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]) or str(e.get("loc", ("body",))[0]),
                     "rule": str(e.get("type", "invalid"))} for e in exc.errors()][:20]
        return error_response(request, 422, "validation_error", "The request is not valid.", details={"problems": problems})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return error_response(request, exc.status_code, code, "Request could not be processed.",
                              headers=dict(exc.headers) if exc.headers else None)

    @app.exception_handler(StoreError)
    async def _store(request: Request, exc: StoreError) -> JSONResponse:
        logger.error("storage failure (%s) on %s", exc.reason, request.url.path)
        return error_response(request, 503, "storage_unavailable", "The operational store is temporarily unavailable.")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled %s on %s", type(exc).__name__, request.url.path)  # type only: never the message
        return error_response(request, 500, "internal_error", "Internal server error.")
