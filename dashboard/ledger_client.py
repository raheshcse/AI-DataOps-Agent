"""Read-only client for the VerbaLedger Explorer. GET only, and only under ``/ledger``: it cannot reach incidents,
approvals, recoveries or tickets, and it has no method that could change anything."""

from __future__ import annotations

from typing import Any

import httpx

from dashboard.http_core import HttpCore, Result, seg


class LedgerClient:
    def __init__(self, base_url: str, token: str | None, *, http: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self._core = HttpCore(base_url, token, prefixes=("/ledger",), methods=("GET",), http=http, timeout=timeout)
        self.base_url = self._core.base_url

    def __repr__(self) -> str:
        return f"LedgerClient({self._core!r})"

    @property
    def has_token(self) -> bool:
        return self._core.has_token

    def summary(self, *, refresh: bool = False) -> Result:
        return self._core.get("/ledger/summary", refresh="true" if refresh else None)

    def events(self, *, limit: int = 50, offset: int = 0, order: str = "desc", refresh: bool = False,
               **filters: Any) -> Result:
        return self._core.get("/ledger/events", limit=limit, offset=offset, order=order,
                              refresh="true" if refresh else None, **filters)

    def event(self, entry_id: str) -> Result:
        return self._core.get(f"/ledger/events/{seg(entry_id)}")

    def trace(self, *, entry_id: str | None = None, decision_id: str | None = None, request_id: str | None = None,
              refresh: bool = False) -> Result:
        return self._core.get("/ledger/trace", entry_id=entry_id, decision_id=decision_id, request_id=request_id,
                              refresh="true" if refresh else None)

    def integrity(self) -> Result:
        return self._core.get("/ledger/integrity")

    def audit(self, *, max_monitor_gap_seconds: float | None = None) -> Result:
        return self._core.get("/ledger/audit", max_monitor_gap_seconds=max_monitor_gap_seconds)


def from_environment(http: httpx.Client | None = None) -> LedgerClient:
    from dashboard import config

    return LedgerClient(config.api_base_url(), config.explorer_token(), http=http, timeout=config.request_timeout())
