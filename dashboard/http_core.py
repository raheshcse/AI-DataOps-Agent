"""The one HTTP layer both UIs use. Small, strict and honest about failure.

* Only paths under an allow-listed prefix, and only allow-listed methods, can be requested (a UI bug cannot reach an
  unrelated endpoint). Path segments are URL-quoted by the callers via :func:`seg`.
* Redirects are never followed (so a token is never forwarded to another host).
* A request is sent AT MOST ONCE. There is no retry anywhere: an uncertain outcome is shown as uncertain.
* The token lives only in a private attribute, is excluded from ``repr`` and is never placed in a message.
* Failures become a :class:`Problem` with a stable ``kind``; missing data is never converted into zero or empty data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import quote

import httpx

MAX_MESSAGE = 300


def seg(value: str) -> str:
    """Quote one path segment so an identifier can never add path components."""
    return quote(str(value), safe="")


@dataclass(frozen=True)
class Problem:
    kind: str  # unreachable | timeout | unauthenticated | forbidden | not_found | validation | rate_limited | unavailable | server_error | client_error | bad_response
    message: str
    status: int | None = None
    code: str | None = None
    request_id: str | None = None
    retry_after: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None  # a non-error-format JSON body (the readiness endpoint answers 503 with its checks)

    @property
    def outcome_unknown(self) -> bool:
        """True when a state-changing request may or may not have been applied."""
        return self.kind in ("timeout", "unreachable", "bad_response", "server_error")


@dataclass(frozen=True)
class Result:
    data: Any = None
    problem: Problem | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


_KIND_BY_STATUS = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 422: "validation", 429: "rate_limited",
                   503: "unavailable"}


def problem_from_response(resp: httpx.Response) -> Problem:
    body: Any = None
    try:
        body = resp.json()
    except ValueError:
        pass
    err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    kind = _KIND_BY_STATUS.get(resp.status_code) or ("server_error" if resp.status_code >= 500 else "client_error")
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    return Problem(kind=kind, message=str(err.get("message") or f"The API answered HTTP {resp.status_code}.")[:MAX_MESSAGE],
                   status=resp.status_code, code=str(err["code"])[:80] if err.get("code") else None,
                   request_id=str(err["request_id"])[:80] if err.get("request_id") else resp.headers.get("x-request-id"),
                   retry_after=resp.headers.get("retry-after"), details=details,
                   body=body if isinstance(body, dict) and "error" not in body else None)


class HttpCore:
    def __init__(self, base_url: str, token: str | None, *, prefixes: Iterable[str], methods: Iterable[str] = ("GET",),
                 http: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._prefixes = tuple(prefixes)
        self._methods = frozenset(m.upper() for m in methods)
        self._http = http if http is not None else httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_http = http is None
        self.timeout = timeout

    def __repr__(self) -> str:  # never includes the token
        return f"HttpCore(base_url={self.base_url!r}, token={'set' if self._token else 'unset'})"

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _check(self, method: str, path: str) -> None:
        if method not in self._methods:
            raise ValueError(f"method {method} is not permitted for this client")
        segments = path.split("/")
        if (not path.startswith("/") or "//" in path or ".." in segments or "?" in path or "#" in path
                or not any(path == p or path.startswith(p + "/") for p in self._prefixes)):
            raise ValueError("path is not permitted for this client")

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None,
                token: str | None = None) -> Result:
        method = method.upper()
        self._check(method, path)
        use_token = token if token is not None else self._token
        headers = {"Accept": "application/json"}
        if use_token:
            headers["Authorization"] = f"Bearer {use_token}"
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        try:
            extra = {"timeout": self.timeout} if self._owns_http else {}  # an injected client keeps its own timeout
            resp = self._http.request(method, self.base_url + path, params=clean or None, json=json, headers=headers,
                                      follow_redirects=False, **extra)
        except httpx.TimeoutException:
            return Result(problem=Problem("timeout", "The API did not answer in time."))
        except httpx.TransportError:
            return Result(problem=Problem("unreachable", "The API could not be reached."))
        except httpx.HTTPError:
            return Result(problem=Problem("unreachable", "The request to the API failed."))
        if resp.status_code >= 400 or 300 <= resp.status_code < 400:
            return Result(problem=problem_from_response(resp))
        try:
            return Result(data=resp.json())
        except ValueError:
            return Result(problem=Problem("bad_response", "The API answered with something that is not JSON.", status=resp.status_code))

    def get(self, path: str, **params: Any) -> Result:
        return self.request("GET", path, params=params)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
