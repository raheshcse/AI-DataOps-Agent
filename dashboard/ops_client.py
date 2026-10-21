"""Typed calls for the Operations Dashboard. Reads, plus four state-changing relays to existing authenticated
endpoints: a human approval decision, starting a read-only investigation, and the idempotent import of a simulator
incident into the workflow store, and requesting a recovery PROPOSAL (a pending approval, never an execution). The server applies the permission check for each (the caller's OWN token is used).
It never calls a ledger endpoint, never executes, resumes or retries a recovery, and never sends a request twice."""

from __future__ import annotations

from typing import Any

import httpx

from dashboard.http_core import HttpCore, Result, seg

READ_PREFIXES = ("/health", "/ready", "/incidents", "/investigations", "/recoveries", "/approvals", "/simulator", "/demo",
                 "/reviews", "/dashboard")


class OpsClient:
    def __init__(self, base_url: str, token: str | None, *, http: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self._core = HttpCore(base_url, token, prefixes=READ_PREFIXES, methods=("GET", "POST"), http=http, timeout=timeout)
        self.base_url = self._core.base_url

    def __repr__(self) -> str:
        return f"OpsClient({self._core!r})"

    @property
    def has_token(self) -> bool:
        return self._core.has_token

    # ---- reads
    def health(self) -> Result:
        return self._core.get("/health")

    def ready(self) -> Result:
        return self._core.get("/ready")

    def incidents(self, *, limit: int = 100, offset: int = 0) -> Result:
        return self._core.get("/incidents", limit=limit, offset=offset)

    def incident(self, incident_id: str) -> Result:
        return self._core.get(f"/incidents/{seg(incident_id)}")

    def status(self, incident_id: str) -> Result:
        return self._core.get(f"/incidents/{seg(incident_id)}/status")

    def investigations(self, incident_id: str) -> Result:
        return self._core.get(f"/incidents/{seg(incident_id)}/investigations")

    def investigation(self, investigation_id: str) -> Result:
        return self._core.get(f"/investigations/{seg(investigation_id)}")

    def recoveries(self, incident_id: str) -> Result:
        return self._core.get(f"/incidents/{seg(incident_id)}/recoveries")

    def recovery(self, recovery_id: str) -> Result:
        return self._core.get(f"/recoveries/{seg(recovery_id)}")

    def approval(self, request_id: str) -> Result:
        return self._core.get(f"/approvals/{seg(request_id)}")

    def ticket(self, incident_id: str) -> Result:
        return self._core.get(f"/incidents/{seg(incident_id)}/ticket")

    # ---- Phase 11 reads: simulator (SQL Server) source, synthetic demo dataset, Human Review queue, summary
    def dashboard_summary(self) -> Result:
        return self._core.get("/dashboard/summary")

    def simulator_incidents(self, *, limit: int = 100, offset: int = 0, q: str | None = None, severity: str | None = None,
                            status: str | None = None, imported: bool | None = None) -> Result:
        return self._core.get("/simulator/incidents", limit=limit, offset=offset, q=q, severity=severity, status=status,
                              imported=None if imported is None else ("true" if imported else "false"))

    def simulator_incident(self, incident_id: str) -> Result:
        return self._core.get(f"/simulator/incidents/{seg(incident_id)}")

    def simulator_pipelines(self) -> Result:
        return self._core.get("/simulator/pipelines")

    def simulator_runs(self, *, pipeline_id: str | None = None, status: str | None = None, limit: int = 25) -> Result:
        return self._core.get("/simulator/runs", pipeline_id=pipeline_id, status=status, limit=limit)

    def demo_summary(self) -> Result:
        return self._core.get("/demo/summary")

    def demo_incidents(self, *, limit: int = 25, offset: int = 0, **filters: Any) -> Result:
        return self._core.get("/demo/incidents", limit=limit, offset=offset, **filters)

    def demo_incident(self, incident_id: str) -> Result:
        return self._core.get(f"/demo/incidents/{seg(incident_id)}")

    def reviews(self, *, state: str = "pending", q: str | None = None, severity: str | None = None, limit: int = 25, offset: int = 0) -> Result:
        return self._core.get("/reviews", state=state, q=q, severity=severity, limit=limit, offset=offset)

    def reviews_summary(self) -> Result:
        return self._core.get("/reviews/summary")

    def review(self, review_id: str) -> Result:
        return self._core.get(f"/reviews/{seg(review_id)}")

    # ---- the only state-changing call
    def decide(self, request_id: str, decision: str, reason: str, approver_token: str) -> Result:
        """Relay a human decision with the APPROVER'S OWN token. The server authenticates it and checks the per-action
        permission; the body can only say approve/reject. Sent once: a timeout is reported as outcome-unknown."""
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        return self._core.request("POST", f"/approvals/{seg(request_id)}/decision",
                                  json={"decision": decision, "reason": reason}, token=approver_token)

    def import_simulator_incident(self, incident_id: str, token: str) -> Result:
        """Idempotent import of a simulator incident into the workflow store (needs ``incident:create``)."""
        return self._core.request("POST", f"/simulator/incidents/{seg(incident_id)}/import", token=token)

    def start_investigation(self, incident_id: str, token: str) -> Result:
        """Start a read-only investigation (needs ``investigation:run``). The server answers 202 with a pending record and
        runs it in the background; it executes no recovery. Sent once: an unknown outcome must be checked, not retried."""
        return self._core.request("POST", f"/incidents/{seg(incident_id)}/investigations", token=token)

    def request_recovery(self, incident_id: str, token: str) -> Result:
        """Ask the API to create a recovery PROPOSAL from the latest completed investigation (needs ``recovery:request``).
        No body is sent, so the action is derived by the server's code from the evidence. It only creates a pending
        approval request: nothing is executed until an authorised approver decides in Human Review."""
        return self._core.request("POST", f"/incidents/{seg(incident_id)}/recoveries", token=token)


def from_environment(http: httpx.Client | None = None) -> OpsClient:
    from dashboard import config

    return OpsClient(config.api_base_url(), config.dashboard_token(), http=http, timeout=config.request_timeout())
