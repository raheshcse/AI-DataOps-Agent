"""The /api/v1 application (a sub-application mounted by app.main).

It is a separate FastAPI app so that its error format, request-id handling and OpenAPI document are isolated from
the Phase 1-2 routes, whose behaviour and tests are unchanged.
"""

from __future__ import annotations

import re
import uuid
from typing import Callable

from fastapi import FastAPI, Request

from app import __version__
from app.api.v1.errors import install_error_handlers
from app.api.v1.routes import approvals, health, incidents, recoveries, tickets
from app.api.v1.security import ServicesProvider
from app.services.container import Services

_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.-]{8,64}$")


def create_api_app(services_factory: Callable[[], Services]) -> FastAPI:
    api = FastAPI(
        title="AI DataOps Agent API", version=__version__,
        description="Incident investigation, persistence, ticketing and human-approved, VSL-governed recovery. "
                    "Authentication: `Authorization: Bearer <token>`. Local development authentication only; "
                    "see docs/phase8-api-persistence-ticketing.md for production prerequisites.")
    api.state.provider = ServicesProvider(services_factory)
    install_error_handlers(api)

    @api.middleware("http")
    async def request_id(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = supplied if _CLIENT_ID.match(supplied) else "req-" + uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    for module in (health, incidents, approvals, recoveries, tickets):
        api.include_router(module.router)
    return api
