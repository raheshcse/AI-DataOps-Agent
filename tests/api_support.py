"""Helpers for the Phase 8 tests. Deterministic: no network, no OpenAI, no SQL Server, no real credentials.

* The API runs over REAL SQLite stores (operations, approvals), the REAL hash-chained ledger, the REAL Phase 7
  workflow/VSL gates and the REAL offline investigation agent + MCP tools over an in-memory simulation.
* GitHub is a ``httpx.MockTransport`` fake (``FakeGitHub``). Nothing here talks to the real GitHub.
* Tokens are test fixtures only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import OfflineHeuristicModel
from app.api.v1 import create_api_app
from app.integrations import GitHubConfig, GitHubIssuesAdapter
from app.persistence import OperationsDatabase, OperationsStore
from app.recovery import RECOVERY_ACTIONS, StaticTokenAuthenticator
from app.recovery.auth import token_digest
from app.services.container import Services
from app.services.investigations import InvestigationService
from app.services.recoveries import RecoveryService
from app.services.tickets import TicketService
from tests.agent_support import apply_scenario, server_for
from tests.recovery_support import ROLES, TOKENS, Clock, Env, make_env

API_TOKENS = {**TOKENS, "reader": "test-reader-token-0123456789abcd", "investigator": "test-investigator-token-0123456789",
              "operator": "test-operator-token-0123456789ab", "operator2": "test-operator2-token-0123456789a"}
API_ROLES = {**ROLES, "reader": "api_reader", "investigator": "investigator", "operator": "operator", "operator2": "operator"}
GITHUB_TOKEN = "ghp_FAKEFAKEFAKE0123456789abcdefghij"

INCIDENT = {"title": "Duplicate orders in the sales report", "description": "We have duplicate orders: the same order appears twice.",
            "incident_type": "duplicate_records", "severity": "high"}


def make_authenticator(revoked: set[str] = frozenset()) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator([{"subject": f"{name}.user", "token_sha256": token_digest(tok), "roles": [API_ROLES[name]]}
                                     for name, tok in API_TOKENS.items() if name not in revoked])


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKENS[role]}"}


# ----------------------------------------------------------------------------------------- fake GitHub
class FakeGitHub:
    """A stateful stand-in for the three GitHub endpoints the adapter uses. ``mode`` injects faults."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.auth_headers: set[str | None] = set()
        self.mode = "ok"  # ok | timeout | timeout_after_create | unreachable | unauthorized | forbidden | rate_limit | server_error
        self.creates = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def close_issue(self, number: int) -> None:
        next(i for i in self.issues if i["number"] == number)["state"] = "closed"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        self.auth_headers.add(request.headers.get("authorization"))
        mode = self.mode
        if mode == "unreachable":
            raise httpx.ConnectError("unreachable", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "unauthorized":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if mode == "forbidden":
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if mode == "rate_limit":
            return httpx.Response(403, json={"message": "API rate limit exceeded"},
                                  headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(datetime.now(timezone.utc).timestamp()) + 120)})
        if mode == "server_error":
            return httpx.Response(502, json={"message": "Bad gateway"})
        path = request.url.path
        if request.method == "POST" and path.endswith("/issues"):
            payload = json.loads(request.content)
            self.creates += 1
            number = len(self.issues) + 1
            issue = {"id": 9000 + number, "number": number, "html_url": f"https://github.com/acme/dataops/issues/{number}",
                     "state": "open", "title": payload["title"], "body": payload["body"], "labels": [{"name": n} for n in payload.get("labels", [])]}
            self.issues.append(issue)
            if mode == "timeout_after_create":  # the issue EXISTS, but the caller never learns it
                raise httpx.ReadTimeout("timeout after create", request=request)
            return httpx.Response(201, json=issue)
        if request.method == "GET" and path.endswith("/issues"):
            return httpx.Response(200, json=list(self.issues))
        if request.method == "GET" and "/issues/" in path:
            number = int(path.rsplit("/", 1)[1])
            found = next((i for i in self.issues if i["number"] == number), None)
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})


def make_adapter(fake: FakeGitHub) -> GitHubIssuesAdapter:
    return GitHubIssuesAdapter(GitHubConfig(token=GITHUB_TOKEN, owner="acme", repo="dataops"), transport=fake.transport(),
                               sleep=lambda s: None)


# ----------------------------------------------------------------------------------------- the stack
@dataclass
class ApiEnv:
    client: TestClient
    services: Services
    env: Env
    store: OperationsStore
    github: FakeGitHub | None
    ops_path: Path
    tmp_path: Path

    def close(self) -> None:
        try:
            self.client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    # convenience -----------------------------------------------------------------------------
    def post(self, path: str, role: str | None = None, **kwargs: Any) -> httpx.Response:
        extra = kwargs.pop("headers", None) or {}
        return self.client.post("/api/v1" + path, headers={**(auth(role) if role else {}), **extra}, **kwargs)

    def get(self, path: str, role: str | None = None, **kwargs: Any) -> httpx.Response:
        extra = kwargs.pop("headers", None) or {}
        return self.client.get("/api/v1" + path, headers={**(auth(role) if role else {}), **extra}, **kwargs)

    def new_incident(self, role: str = "operator", **overrides: Any) -> dict[str, Any]:
        r = self.post("/incidents", role, json={**INCIDENT, **overrides})
        assert r.status_code in (200, 201), r.text
        return r.json()

    def investigate(self, incident_id: str, role: str = "operator") -> dict[str, Any]:
        r = self.post(f"/incidents/{incident_id}/investigations", role)
        assert r.status_code == 202, r.text
        done = self.get(f"/investigations/{r.json()['investigation_id']}", "reader").json()
        return done  # TestClient finishes background tasks before returning

    def request_recovery(self, incident_id: str, role: str = "operator", **kwargs: Any) -> httpx.Response:
        return self.post(f"/incidents/{incident_id}/recoveries", role, **kwargs)


def build_api(tmp_path: Path, *, name: str = "a", github: FakeGitHub | None = None, recovery: bool = True, sim=None,
              scenario: str | None = "duplicate-records", clock: Clock | None = None,
              allowed: frozenset[str] = RECOVERY_ACTIONS, revoked: set[str] = frozenset(),
              llm_factory: Callable[[], Any] = OfflineHeuristicModel, app_env: str = "test") -> ApiEnv:
    authenticator = make_authenticator(revoked)
    env = make_env(tmp_path, name=name, clock=clock, sim=sim, allowed=allowed, auth=authenticator)
    if sim is None and scenario:
        apply_scenario(env.sim, scenario)
    ops_path = tmp_path / f"{name}-ops.sqlite3"
    database = OperationsDatabase(ops_path)
    database.migrate()
    store = OperationsStore(database)
    store.sweep_interrupted_investigations()
    investigations = InvestigationService(store, llm_factory=llm_factory, mcp_target_factory=lambda: server_for(env.sim))
    tickets = TicketService(store, make_adapter(github) if github else None)
    services = Services(store=store, authenticator=authenticator, investigations=investigations, tickets=tickets, app_env=app_env,
                        auth_status="dev_static", approvals=env.approvals, recorder=env.recorder,
                        recoveries=RecoveryService(store, env.workflow, env.approvals, env.recorder) if recovery else None,
                        recovery_unavailable=None if recovery else "disabled_in_test",
                        ticket_status="configured" if github else "not_configured")
    if services.recoveries:
        services.recoveries.reconcile_all()
    parent = FastAPI()
    parent.mount("/api/v1", create_api_app(lambda: services))  # exactly how app.main mounts it
    client = TestClient(parent, raise_server_exceptions=False)
    client.__enter__()
    return ApiEnv(client, services, env, store, github, ops_path, tmp_path)


@pytest.fixture
def api(tmp_path):
    """``api(**kwargs)`` builds an ApiEnv; every one is closed at teardown."""
    made: list[ApiEnv] = []

    def factory(**kwargs: Any) -> ApiEnv:
        kwargs.setdefault("tmp_path", tmp_path)
        path = kwargs.pop("tmp_path")
        e = build_api(path, **kwargs)
        made.append(e)
        return e

    yield factory
    for e in made:
        e.close()
