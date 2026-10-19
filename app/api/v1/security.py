"""Authentication and authorisation dependencies.

The caller's identity and roles come ONLY from the verified Bearer token (the Phase 7 authenticator). No header or
body field can name or elevate a user. The raw token is kept on the ``Caller`` solely so it can be relayed to the
approval service, which authenticates it again itself; it is excluded from repr() and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, Request

from app.api.v1.errors import ApiError
from app.api.v1.permissions import api_permissions
from app.recovery.auth import Identity
from app.services.container import Services


@dataclass(frozen=True)
class Caller:
    identity: Identity
    credential: str = field(repr=False)

    @property
    def subject(self) -> str:
        return self.identity.subject


class ServicesProvider:
    """Builds the Services once, lazily. A failed build is retried on the next request (nothing is cached)."""

    def __init__(self, factory: Callable[[], Services]) -> None:
        self._factory, self._services = factory, None

    def get(self) -> Services:
        if self._services is None:
            try:
                self._services = self._factory()
            except Exception as exc:  # noqa: BLE001 - generic 503; the type is logged by the caller's handler
                raise ApiError(503, "service_unavailable", "The service is not available.") from exc
        return self._services


def get_services(request: Request) -> Services:
    return request.app.state.provider.get()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def require(permission: str) -> Callable[..., Caller]:
    async def dependency(request: Request, services: Services = Depends(get_services)) -> Caller:
        client = request.client.host if request.client else "unknown"
        wait = services.throttle.retry_after(client)
        if wait is not None:
            raise ApiError(429, "rate_limited", "Too many failed authentication attempts. Try again later.",
                           headers={"Retry-After": str(wait)})
        token = _bearer(request)
        challenge = {"WWW-Authenticate": "Bearer"}
        if token is None:
            raise ApiError(401, "unauthenticated", "Authentication is required.", headers=challenge)
        identity = services.authenticator.authenticate(token)
        if identity is None:
            services.throttle.record_failure(client)
            raise ApiError(401, "unauthenticated", "Authentication failed.", headers=challenge)
        services.throttle.record_success(client)
        if permission not in api_permissions(identity.roles):
            raise ApiError(403, "forbidden", "You do not have permission for this operation.")
        return Caller(identity, token)

    return dependency
