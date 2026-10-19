"""Phase 8 ticketing tests. GitHub is ALWAYS the offline FakeGitHub (httpx.MockTransport): nothing here is a live GitHub test."""

import logging

import httpx
import pytest

from app.integrations import GitHubConfig, GitHubIssuesAdapter, TicketError, incident_marker
from tests.api_support import GITHUB_TOKEN, FakeGitHub, api, auth, make_adapter  # noqa: F401  (api is a fixture)


# ------------------------------------------------------------------------------------------ config
def test_config_absent_means_not_configured_and_nothing_is_invented():
    assert GitHubConfig.from_environ({}) is None
    assert GitHubConfig.from_environ({"GITHUB_TOKEN": "x" * 30, "GITHUB_OWNER": "o"}) is None


@pytest.mark.parametrize("env", [
    {"GITHUB_OWNER": "bad owner", "GITHUB_REPO": "r"},
    {"GITHUB_OWNER": "o", "GITHUB_REPO": "r/../x"},
    {"GITHUB_OWNER": "o", "GITHUB_REPO": "r", "GITHUB_API_URL": "http://insecure.example"},
])
def test_invalid_config_is_rejected_without_echoing_the_token(env):
    with pytest.raises(ValueError) as info:
        GitHubConfig.from_environ({"GITHUB_TOKEN": GITHUB_TOKEN, **env})
    assert GITHUB_TOKEN not in str(info.value)


def test_token_never_appears_in_reprs():
    cfg = GitHubConfig.from_environ({"GITHUB_TOKEN": GITHUB_TOKEN, "GITHUB_OWNER": "acme", "GITHUB_REPO": "dataops"})
    assert cfg.repository == "acme/dataops" and cfg.api_url == "https://api.github.com"
    adapter = GitHubIssuesAdapter(cfg, transport=FakeGitHub().transport())
    assert GITHUB_TOKEN not in repr(cfg) and GITHUB_TOKEN not in repr(adapter) and GITHUB_TOKEN not in str(adapter)


# ------------------------------------------------------------------------------------------ adapter
def test_create_find_and_get_roundtrip():
    fake = FakeGitHub()
    a = make_adapter(fake)
    issue = a.create_issue(title="T", body="body\n" + incident_marker("inc-1"))
    assert (issue.number, issue.state, issue.url) == (1, "open", "https://github.com/acme/dataops/issues/1")
    assert a.find_issue_by_marker(incident_marker("inc-1")).number == 1
    assert a.find_issue_by_marker(incident_marker("inc-2")) is None
    fake.close_issue(1)
    assert a.get_issue(1).state == "closed"
    assert fake.auth_headers == {f"Bearer {GITHUB_TOKEN}"}


@pytest.mark.parametrize("mode,kind,statuscalls", [("unauthorized", "auth_failed", 1), ("forbidden", "permission_denied", 1),
                                                   ("rate_limit", "rate_limited", 1), ("timeout", "timeout", 3),
                                                   ("unreachable", "unavailable", 3), ("server_error", "service_error", 3)])
def test_reads_classify_failures_and_retry_only_where_it_can_help(mode, kind, statuscalls):
    fake = FakeGitHub()
    fake.mode = mode
    with pytest.raises(TicketError) as info:
        make_adapter(fake).get_issue(1)
    assert info.value.kind == kind and len(fake.requests) == statuscalls
    assert GITHUB_TOKEN not in repr(info.value) and "Bad credentials" not in str(info.value)


def test_rate_limit_reports_retry_after():
    fake = FakeGitHub()
    fake.mode = "rate_limit"
    with pytest.raises(TicketError) as info:
        make_adapter(fake).get_issue(1)
    assert 1 <= info.value.retry_after <= 120


@pytest.mark.parametrize("mode", ["timeout", "unreachable", "server_error"])
def test_create_is_never_retried_automatically(mode):
    fake = FakeGitHub()
    fake.mode = mode
    with pytest.raises(TicketError):
        make_adapter(fake).create_issue(title="t", body="b")
    assert [m for m, _ in fake.requests] == ["POST"]


def test_malformed_provider_responses_are_bad_response():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})
    a = GitHubIssuesAdapter(GitHubConfig(token=GITHUB_TOKEN, owner="o", repo="r"), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(TicketError) as info:
        a.get_issue(1)
    assert info.value.kind == "bad_response"


def test_pull_requests_are_not_mistaken_for_incident_issues():
    def handler(request):
        return httpx.Response(200, json=[{"id": 1, "number": 1, "html_url": "u", "state": "open", "pull_request": {}, "body": incident_marker("inc-1")}])
    a = GitHubIssuesAdapter(GitHubConfig(token=GITHUB_TOKEN, owner="o", repo="r"), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert a.find_issue_by_marker(incident_marker("inc-1")) is None


# ------------------------------------------------------------------------------------------ through the API
def test_ticket_creation_association_and_duplicate_prevention(api):
    e = api(github=FakeGitHub())
    inc = e.new_incident()
    iid = inc["incident_id"]
    e.investigate(iid)
    first = e.post(f"/incidents/{iid}/ticket", "operator")
    assert first.status_code == 201
    t = first.json()
    assert (t["number"], t["status"], t["repository"], t["provider"]) == (1, "open", "acme/dataops", "github_issues")
    assert t["url"] == "https://github.com/acme/dataops/issues/1"
    second = e.post(f"/incidents/{iid}/ticket", "operator")
    assert second.status_code == 200 and second.json()["association_id"] == t["association_id"]
    assert e.github.creates == 1 and len(e.github.issues) == 1
    assert incident_marker(iid) in e.github.issues[0]["body"]
    got = e.get(f"/incidents/{iid}/ticket", "reader").json()
    assert got["number"] == 1 and [ev["outcome"] for ev in got["recent_events"]] == ["created"]


def test_a_second_incident_never_adopts_the_first_incidents_issue(api):
    e = api(github=FakeGitHub())
    a, b = e.new_incident()["incident_id"], e.new_incident(title="A different incident title")["incident_id"]
    assert e.post(f"/incidents/{a}/ticket", "operator").status_code == 201
    assert e.post(f"/incidents/{b}/ticket", "operator").json()["number"] == 2


def test_sync_reflects_closure_but_a_closed_issue_proves_nothing(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.post(f"/incidents/{iid}/ticket", "operator")
    e.github.close_issue(1)
    synced = e.post(f"/incidents/{iid}/ticket/sync", "operator").json()
    assert synced["status"] == "closed" and synced["last_sync_result"] == "ok"
    assert e.get(f"/incidents/{iid}", "reader").json()["status"] == "new"           # incident state untouched
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "new"
    assert e.store.list_recoveries(iid) == []


def test_unconfirmed_create_is_adopted_on_retry_without_a_duplicate(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.github.mode = "timeout_after_create"
    r = e.post(f"/incidents/{iid}/ticket", "operator")
    assert r.status_code == 503 and r.json()["error"]["code"] == "ticket_provider_timeout"
    assert r.json()["error"]["details"]["incident_preserved"] is True
    assert e.store.get_ticket(iid)["status"] == "creation_unconfirmed" and e.store.get_ticket(iid)["external_number"] is None
    e.github.mode = "ok"
    retry = e.post(f"/incidents/{iid}/ticket", "operator")
    assert retry.status_code == 200 and retry.json()["number"] == 1  # adopted: nothing was created now
    assert e.github.creates == 1 and len(e.github.issues) == 1                       # adopted, not recreated
    assert [ev["outcome"] for ev in e.store.list_ticket_events(iid)][0] == "adopted_existing"


@pytest.mark.parametrize("mode,status,code", [("timeout", 503, "ticket_provider_timeout"), ("unreachable", 503, "ticket_provider_unavailable"),
                                              ("rate_limit", 503, "ticket_provider_rate_limited"), ("server_error", 503, "ticket_provider_service_error"),
                                              ("unauthorized", 502, "ticket_provider_auth_failed"), ("forbidden", 502, "ticket_provider_permission_denied")])
def test_provider_failures_preserve_the_incident_and_never_fabricate_a_ticket(api, mode, status, code):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.github.mode = mode
    r = e.post(f"/incidents/{iid}/ticket", "operator")
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert GITHUB_TOKEN not in r.text and "Bad credentials" not in r.text
    assert e.get(f"/incidents/{iid}", "reader").status_code == 200                 # incident preserved
    row = e.store.get_ticket(iid)
    assert row["external_id"] is None and row["url"] is None and row["external_number"] is None
    if mode == "rate_limit":
        assert int(r.headers["Retry-After"]) >= 1
    e.github.mode = "ok"                                                           # safe retry once GitHub is back
    assert e.post(f"/incidents/{iid}/ticket", "operator").status_code == 201


def test_failed_sync_keeps_the_last_known_state(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.post(f"/incidents/{iid}/ticket", "operator")
    e.github.mode = "timeout"
    assert e.post(f"/incidents/{iid}/ticket/sync", "operator").status_code == 503
    row = e.store.get_ticket(iid)
    assert row["status"] == "open" and row["last_sync_result"] == "timeout" and row["url"].endswith("/1")


def test_unconfigured_ticketing_is_unavailable_and_invents_nothing(api):
    e = api(github=None)
    iid = e.new_incident()["incident_id"]
    r = e.post(f"/incidents/{iid}/ticket", "operator")
    assert r.status_code == 503 and r.json()["error"]["code"] == "ticketing_unavailable"
    assert e.get(f"/incidents/{iid}/ticket", "reader").status_code == 404
    assert e.post(f"/incidents/{iid}/ticket/sync", "operator").status_code == 503
    assert e.store.get_ticket(iid) is None


def test_sync_without_a_ticket_is_404_and_unknown_incident_is_404(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    assert e.post(f"/incidents/{iid}/ticket/sync", "operator").status_code == 404
    assert e.post("/incidents/inc-missing/ticket", "operator").status_code == 404


def test_ticket_permissions(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    assert e.post(f"/incidents/{iid}/ticket").status_code == 401
    for role in ("reader", "investigator", "approver", "viewer"):
        assert e.post(f"/incidents/{iid}/ticket", role).status_code == 403, role
    assert e.github.creates == 0
    assert e.get(f"/incidents/{iid}/ticket", "reader").status_code == 404           # reading is allowed, there is just no ticket yet


def test_the_token_is_never_logged_or_returned(api, caplog):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    with caplog.at_level(logging.DEBUG):
        bodies = [e.post(f"/incidents/{iid}/ticket", "operator").text, e.get(f"/incidents/{iid}/ticket", "reader").text,
                  e.post(f"/incidents/{iid}/ticket/sync", "operator").text]
        e.github.mode = "unauthorized"
        bodies.append(e.post(f"/incidents/{iid}/ticket/sync", "operator").text)
    assert all(GITHUB_TOKEN not in b for b in bodies) and GITHUB_TOKEN not in caplog.text
