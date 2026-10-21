"""The root OpenAPI document lists the mounted /api/v1 API (the reported "only /sim, /health, /ready" defect)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_root_openapi_exposes_versioned_api_and_keeps_existing_routes():
    with TestClient(app) as c:
        doc = c.get("/openapi.json").json()
        assert c.get("/docs").status_code == 200
    paths = set(doc["paths"])
    for expected in ("/health", "/ready", "/sim/pipelines", "/sim/runs", "/api/v1/incidents", "/api/v1/incidents/{incident_id}",
                     "/api/v1/approvals/{request_id}", "/api/v1/ledger/summary", "/api/v1/ledger/events", "/api/v1/simulator/incidents"):
        assert expected in paths, expected
    assert not any(p.startswith("/api/v1/api/v1") for p in paths)
    assert "HTTPValidationError" in doc["components"]["schemas"]


def test_documenting_does_not_change_routing_or_auth():
    with TestClient(app) as c:
        assert c.get("/api/v1/incidents").status_code == 401  # still authenticated
        assert c.get("/health").status_code == 200
