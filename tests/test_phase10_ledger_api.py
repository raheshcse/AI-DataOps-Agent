"""Phase 10: the read-only VerbaLedger Explorer API (/api/v1/ledger/*).

Real hash-chained ledger, real Phase 7 workflow and VSL gates; no network. Every test that reads asserts the ledger
file is byte-for-byte unchanged afterwards where relevant."""

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.governance import GateInput, ledger_view as lv
from tests.api_support import api, auth  # noqa: F401  (api is a fixture)
from tests.recovery_support import run

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = ("viewer", "reader", "admin", "approver")
DENIED = ("investigator", "operator", "authority")
ENDPOINTS = ["/ledger/summary", "/ledger/events", "/ledger/events/x", "/ledger/trace?entry_id=x", "/ledger/integrity", "/ledger/audit"]


def fingerprint(path: Path):
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size


def flow(e):
    """An incident, investigation, recovery request and an approval -> a real multi-event Phase 7 trail."""
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    req = rec["approval_request_id"]
    r = e.post(f"/approvals/{req}/decision", "admin", json={"decision": "approve", "reason": "checked"})
    assert r.status_code == 200, r.text
    return iid, rec, req


def violate(e):
    run(e.env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                        cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))


# ------------------------------------------------------------------------------------------- authentication / authorisation
@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_ledger_endpoint_requires_authentication(api, path):
    e = api()
    flow(e)
    assert e.get(path).status_code == 401
    bad = e.get(path, headers={"Authorization": "Bearer not-a-real-token-0123456789abcdef"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("role", DENIED)
@pytest.mark.parametrize("path", ENDPOINTS)
def test_roles_without_ledger_read_are_forbidden(api, role, path):
    e = api()
    r = e.get(path, role)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("role", ALLOWED)
def test_roles_with_ledger_read_may_list(api, role):
    e = api()
    flow(e)
    assert e.get("/ledger/events", role).status_code == 200


# ------------------------------------------------------------------------------------------- events, pagination, filters
def test_events_are_paginated_ordered_and_bounded(api):
    e = api()
    flow(e)
    body = e.get("/ledger/events?limit=2&order=asc", "viewer").json()
    assert len(body["items"]) == 2 and body["total"] >= 4 and body["limit"] == 2 and body["offset"] == 0
    assert [i["sequence"] for i in body["items"]] == [0, 1]
    nxt = e.get("/ledger/events?limit=2&offset=2&order=asc", "viewer").json()["items"]
    assert [i["sequence"] for i in nxt] == [2, 3]
    desc = e.get("/ledger/events?limit=1", "viewer").json()["items"][0]
    assert desc["sequence"] == body["total"] - 1
    assert e.get("/ledger/events?limit=500&offset=10000", "viewer").status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "order=sideways", "entry_type=NOPE",
                                   "decision_id=has space", "from_time=yesterday-ish", "outcome=a%3Bdrop"])
def test_malformed_query_parameters_are_rejected_in_the_standard_error_format(api, query):
    e = api()
    r = e.get(f"/ledger/events?{query}", "viewer")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert "request_id" in r.json()["error"]


def test_filters_select_only_recorded_values(api):
    e = api()
    iid, rec, req = flow(e)
    every = e.get("/ledger/events?limit=200&order=asc", "viewer").json()
    outcomes = {i["outcome"] for i in every["items"]}
    assert {"recovery_proposed", "approval_requested", "approval_granted"} <= outcomes
    one = e.get("/ledger/events?outcome=approval_granted", "viewer").json()
    assert one["total"] == 1 and one["items"][0]["request_id"] == req
    by_req = e.get(f"/ledger/events?request_id={req}&limit=200", "viewer").json()
    assert by_req["total"] >= 2 and all(i["request_id"] == req or i["decision_id"] == req for i in by_req["items"])
    by_inc = e.get(f"/ledger/events?incident_id={iid}", "viewer").json()
    assert by_inc["total"] >= 1 and all(i["incident_id"] == iid for i in by_inc["items"])
    assert e.get("/ledger/events?incident_id=INC-DOES-NOT-EXIST", "viewer").json()["total"] == 0
    assert e.get("/ledger/events?entry_type=TERMINAL", "viewer").json()["total"] == 0
    first = every["items"][0]
    window = e.get("/ledger/events", "viewer", params={"from_time": "2999-01-01T00:00:00Z"}).json()
    assert window["total"] == 0 and first["time"]


def test_rule_and_terminal_fields_appear_only_where_recorded(api):
    e = api()
    violate(e)
    items = e.get("/ledger/events?order=asc&limit=50", "viewer").json()["items"]
    assert [i["entry_type"] for i in items] == ["MONITOR", "VERIFICATION", "TERMINAL"]
    assert items[0]["rule_id"] == "action_within_allowlist" and items[0]["outcome"] == "invariant_violated"
    assert items[1]["result"] == "INSUFFICIENT" and items[2]["terminal_state"] == "automation_suspended"
    assert e.get("/ledger/events?rule_id=action_within_allowlist", "viewer").json()["total"] == 3
    assert e.get("/ledger/summary", "viewer").json()["suspended"] is True


def test_event_detail_has_hashes_and_bounded_redacted_payload(api):
    e = api()
    flow(e)
    first = e.get("/ledger/events?order=asc&limit=1", "viewer").json()["items"][0]
    d = e.get(f"/ledger/events/{first['entry_id']}", "viewer").json()["event"]
    assert d["entry_hash"] and "prev_hash" in d and isinstance(d["payload"], dict) and d["payload_truncated"] is False
    miss = e.get("/ledger/events/not-an-event", "viewer")
    assert miss.status_code == 404 and miss.json()["error"]["code"] == "ledger_event_not_found"


def test_payload_redaction_and_size_bounds():
    cleaned = lv.safe_payload({"api_token": "abc", "nested": {"Authorization": "Bearer z", "ok": 1}, "note": "x" * 5000})
    assert cleaned["api_token"] == "[REDACTED]" and cleaned["nested"]["Authorization"] == "[REDACTED]"
    assert len(cleaned["note"]) < 2100
    big, truncated = lv.bounded_payload({f"k{i}": "y" * 1500 for i in range(40)})
    assert truncated is True and len(json.dumps(big)) <= lv.MAX_PAYLOAD_BYTES + 100


# ------------------------------------------------------------------------------------------- trace
def test_trace_follows_only_recorded_caused_by_links(api):
    e = api()
    iid, rec, req = flow(e)
    events = e.get("/ledger/events?limit=200&order=asc", "viewer").json()["items"]
    seed = next(i for i in events if i["outcome"] == "approval_granted")
    t = e.get("/ledger/trace", "viewer", params={"entry_id": seed["entry_id"]}).json()
    ids = {n["entry_id"] for n in t["nodes"]}
    by_id = {n["entry_id"]: n for n in t["nodes"]}
    assert seed["entry_id"] in ids and t["edges"]
    for edge in t["edges"]:
        assert edge["basis"] == "caused_by" and by_id[edge["effect"]]["caused_by"] == edge["cause"]
    assert set(t["chronology"]) == ids and "not causal evidence" in " ".join(t["notes"])
    assert e.get("/ledger/trace?request_id=" + req, "viewer").status_code == 200


def test_trace_requires_exactly_one_identifier_and_404s_unknown(api):
    e = api()
    flow(e)
    assert e.get("/ledger/trace", "viewer").status_code == 422
    assert e.get("/ledger/trace?entry_id=a&decision_id=b", "viewer").status_code == 422
    r = e.get("/ledger/trace?decision_id=dec-unknown", "viewer")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ledger_trace_not_found"


def test_trace_flags_a_missing_cause_and_never_invents_one(api):
    e = api()
    e.services.recorder.monitor(decision_id="dec-orphan0001", drift=False, caused_by="ent-does-not-exist", outcome="recovery_succeeded")
    t = e.get("/ledger/trace?decision_id=dec-orphan0001", "viewer").json()
    assert t["incomplete"] is True and t["edges"] == []
    assert t["missing_links"][0]["missing_cause"] == "ent-does-not-exist"


def test_trace_marks_an_attempt_without_a_result_as_uncertain(api):
    e = api()
    entry = e.services.recorder.monitor(decision_id="apr-uncertain01", drift=False, outcome="recovery_attempt", request_id="apr-uncertain01")
    t = e.get("/ledger/trace?request_id=apr-uncertain01", "viewer").json()
    assert t["uncertain_attempts"] == [entry.entry_id] and t["incomplete"] is True
    e.services.recorder.monitor(decision_id="apr-uncertain01", drift=False, caused_by=entry.entry_id, outcome="recovery_succeeded", request_id="apr-uncertain01")
    done = e.get("/ledger/trace?request_id=apr-uncertain01&refresh=true", "viewer").json()
    assert done["uncertain_attempts"] == []


def test_chronology_is_not_used_as_causality(api):
    e = api()
    a = e.services.recorder.monitor(decision_id="dec-aaaaaaaa0001", drift=False, outcome="x_one")
    e.services.recorder.monitor(decision_id="dec-aaaaaaaa0001", drift=False, outcome="x_two")
    t = e.get("/ledger/trace?decision_id=dec-aaaaaaaa0001", "viewer").json()
    assert t["edges"] == [] and len(t["roots"]) == 2 and len(t["chronology"]) == 2 and a.entry_id in t["roots"]


# ------------------------------------------------------------------------------------------- malformed / unavailable
def test_malformed_lines_are_reported_never_silently_dropped(api):
    e = api()
    flow(e)
    path = e.env.ledger_path
    with path.open("ab") as fh:
        fh.write(b"{this is not json}\n")
    body = e.get("/ledger/events?limit=200", "viewer").json()
    view = body["view"]
    assert view["complete"] is False and view["malformed_count"] == 1 and view["malformed"][0]["reason"] == "invalid JSON"
    assert body["total"] >= 4  # the readable records are still shown
    s = e.get("/ledger/summary", "viewer").json()
    assert s["view"]["complete"] is False


def test_integrity_reports_the_first_failing_record_for_a_corrupt_line(api):
    e = api()
    flow(e)
    lines = e.env.ledger_path.read_bytes().splitlines(keepends=True)
    with e.env.ledger_path.open("ab") as fh:
        fh.write(b"garbage\n")
    r = e.get("/ledger/integrity", "viewer").json()
    assert r["status"] == "failed" and r["first_failure"]["kind"] == "unparseable_record"
    assert r["first_failure"]["line"] == len(lines) + 1 and r["records_examined"] == len(lines)
    assert r["limits"] and "truncat" in " ".join(r["limits"]).lower()


def test_partially_written_final_line_is_could_not_complete(api):
    e = api()
    flow(e)
    with e.env.ledger_path.open("ab") as fh:
        fh.write(b'{"entry_id": "half')
    r = e.get("/ledger/integrity", "viewer").json()
    assert r["status"] == "could_not_complete" and r["errors"] and r["first_failure"]["line"]


def test_tampering_is_detected_with_line_and_sequence(api):
    e = api()
    flow(e)
    path = e.env.ledger_path
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows[1]["payload"]["tampered"] = True
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    r = e.get("/ledger/integrity", "viewer").json()
    assert r["status"] == "failed" and r["first_failure"]["kind"] == "entry_hash_mismatch"
    assert (r["first_failure"]["line"], r["first_failure"]["sequence"]) == (2, 1) and r["records_examined"] == 1
    a = e.get("/ledger/audit", "viewer")
    assert a.status_code == 200 and a.json()["integrity_passed"] is False  # reported, not hidden


def test_a_clean_ledger_verifies_and_reports_a_checkpoint(api):
    e = api()
    flow(e)
    r = e.get("/ledger/integrity", "viewer").json()
    assert r["status"] == "verified" and r["first_failure"] is None and r["verified_at"]
    assert r["checkpoint"]["sequence"] == r["records_examined"] - 1


def test_missing_ledger_is_503_and_the_file_is_not_created(api):
    e = api()
    flow(e)
    e.env.ledger_path.unlink()
    for path in ("/ledger/events", "/ledger/summary", "/ledger/integrity", "/ledger/audit", "/ledger/trace?entry_id=a"):
        r = e.get(path, "viewer")
        assert r.status_code == 503 and r.json()["error"]["code"] == "ledger_unavailable", path
    assert not e.env.ledger_path.exists()


def test_no_ledger_configured_is_503(api):
    e = api()
    e.services.recorder = None
    assert e.get("/ledger/events", "viewer").status_code == 503


def test_scan_limit_marks_the_view_incomplete(api, monkeypatch):
    e = api()
    flow(e)
    monkeypatch.setenv("LEDGER_MAX_SCAN_LINES", "2")
    lv._READERS.clear()
    body = e.get("/ledger/events", "viewer").json()
    assert body["view"]["scan_truncated"] is True and body["view"]["complete"] is False and body["total"] == 2
    assert e.get("/ledger/integrity", "viewer").json()["status"] == "could_not_complete"


# ------------------------------------------------------------------------------------------- cache honesty
def test_cache_is_invalidated_by_file_change_and_refresh_is_explicit(api):
    e = api()
    iid, rec, req = flow(e)
    first = e.get("/ledger/events", "viewer").json()
    assert first["view"]["cached"] is False
    again = e.get("/ledger/events", "viewer").json()
    assert again["view"]["cached"] is True and again["view"]["as_of"] == first["view"]["as_of"]
    forced = e.get("/ledger/events?refresh=true", "viewer").json()
    assert forced["view"]["cached"] is False
    e.services.recorder.monitor(decision_id="dec-new000000001", drift=False, outcome="later_event")
    changed = e.get("/ledger/events", "viewer").json()
    assert changed["view"]["cached"] is False and changed["total"] == first["total"] + 1


def test_integrity_and_audit_are_never_served_from_cache(api):
    e = api()
    flow(e)
    assert e.get("/ledger/integrity", "viewer").json()["status"] == "verified"
    rows = [json.loads(x) for x in e.env.ledger_path.read_text().splitlines()]
    rows[0]["payload"]["tampered"] = 1
    e.env.ledger_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert e.get("/ledger/integrity", "viewer").json()["status"] == "failed"
    assert e.get("/ledger/events", "viewer", params={"limit": 1}).headers["cache-control"] == "no-store"


def test_suspension_is_recomputed_not_cached(api):
    e = api()
    flow(e)
    assert e.get("/ledger/summary", "viewer").json()["suspended"] is False
    violate(e)
    assert e.get("/ledger/summary", "viewer").json()["suspended"] is True


# ------------------------------------------------------------------------------------------- audit
def test_semantic_audit_reports_each_check_and_distinguishes_itself_from_integrity(api):
    e = api()
    flow(e)
    a = e.get("/ledger/audit", "viewer").json()
    by = {c["name"]: c for c in a["checks"]}
    assert set(by) == set(lv.AUDIT_CHECKS) and a["status"] == "passed"
    assert by["no_monitoring_gaps"]["status"] == "not_run"
    assert by["recovery_attempt_has_result"]["status"] == "passed" and by["recovery_attempt_has_result"]["examined"] >= 1
    assert any("SEMANTIC" in n for n in a["notes"])
    gap = e.get("/ledger/audit?max_monitor_gap_seconds=86400", "viewer").json()
    assert {c["name"]: c for c in gap["checks"]}["no_monitoring_gaps"]["status"] == "passed"
    assert e.get("/ledger/audit?max_monitor_gap_seconds=-1", "viewer").status_code == 422


def test_audit_failure_for_an_open_terminal_lists_the_events(api):
    e = api()
    violate(e)
    a = e.get("/ledger/audit", "viewer").json()
    term = {c["name"]: c for c in a["checks"]}["terminal_has_human_authorised_transition"]
    assert a["status"] == "failed" and term["status"] == "failed" and term["violations"]
    assert term["events"][0]["entry_type"] == "TERMINAL"


def test_audit_on_a_corrupt_ledger_says_could_not_complete(api):
    e = api()
    flow(e)
    with e.env.ledger_path.open("ab") as fh:
        fh.write(b"garbage\n")
    a = e.get("/ledger/audit", "viewer").json()
    assert a["status"] == "could_not_complete" and a["error"] and a["checks"] == []


# ------------------------------------------------------------------------------------------- read-only guarantees
def test_every_ledger_endpoint_leaves_the_ledger_byte_identical(api):
    e = api()
    flow(e)
    violate(e)
    before = fingerprint(e.env.ledger_path)
    for path in ENDPOINTS + ["/ledger/events?refresh=true", "/ledger/audit?max_monitor_gap_seconds=5"]:
        for role in ("viewer", None, "operator"):
            e.get(path, role)
    assert fingerprint(e.env.ledger_path) == before


def test_responses_never_contain_filesystem_paths_or_tokens(api):
    e = api()
    flow(e)
    violate(e)
    blobs = []
    for path in ("/ledger/summary", "/ledger/events?limit=200", "/ledger/integrity", "/ledger/audit"):
        blobs.append(e.get(path, "viewer").text)
    blobs.append(e.get("/ledger/events/does-not-exist", "viewer").text)
    text = "\n".join(blobs)
    assert str(e.env.ledger_path) not in text and str(e.tmp_path) not in text and ".jsonl" not in text
    from tests.api_support import API_TOKENS

    assert not any(tok in text for tok in API_TOKENS.values())
    assert '"ledger"' not in text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/ledger/events", "/ledger/integrity", "/ledger/audit", "/ledger/suspension", "/ledger/reenable"])
def test_there_is_no_write_route_under_ledger(api, method, path):
    e = api()
    flow(e)
    before = fingerprint(e.env.ledger_path)
    r = getattr(e.client, method)("/api/v1" + path, headers=auth("admin"))
    assert r.status_code in (404, 405)
    assert fingerprint(e.env.ledger_path) == before


def test_the_client_cannot_choose_the_file(api):
    e = api()
    flow(e)
    r = e.get("/ledger/events?path=/etc/passwd&ledger=/tmp/x.jsonl&file=../../x", "viewer")
    assert r.status_code == 200  # unknown parameters are ignored: the server-configured ledger is always the one read
    assert e.get("/ledger/trace?entry_id=../../etc/passwd", "viewer").status_code == 422


def test_ledger_routes_are_get_only_and_the_module_has_no_write_paths():
    from app.api.v1.routes.ledger import router

    ledger_methods = set()
    for r in router.routes:
        assert r.path.startswith("/ledger")
        ledger_methods |= set(r.methods or ())
    assert ledger_methods <= {"GET", "HEAD"} and ledger_methods
    for module in ("app/api/v1/routes/ledger.py", "app/governance/ledger_view.py"):
        source = (ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_calls = {"write_text", "write_bytes", "unlink", "truncate", "touch", "rename", "rmdir", "mkdir"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned_calls, f"{module}: {node.func.attr}"
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
                assert not any(isinstance(a, ast.Constant) and set("wa+x") & set(str(a.value)) for a in node.args[1:2]), module
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {node.module or ""} | {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        forbidden = {"RecoveryRunner", "RecoveryWorkflow", "GovernedActionService", "ApprovalService", "GovernanceAuthority",
                     "request_re_enablement", "app.recovery", "app.services.recoveries"}
        assert not imported & forbidden, (module, imported & forbidden)
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        banned_methods = {"write", "re_enable", "approve", "reject", "decide", "execute", "monitor", "verification"}
        if module.endswith("routes/ledger.py"):  # (the view module uses in-memory list.append)
            banned_methods |= {"append"}
        assert not called & banned_methods, module


def test_reader_never_creates_a_missing_file(tmp_path):
    missing = tmp_path / "nope.jsonl"
    reader = lv.LedgerReader(missing)
    with pytest.raises(FileNotFoundError):
        reader.snapshot()
    assert reader.verify()["status"] == "could_not_complete" and not missing.exists()
    assert not (tmp_path / "nope.jsonl.lock").exists()


def test_permissions_are_a_separate_boundary():
    from app.api.v1.permissions import API_PERMISSIONS

    holders = {role for role, perms in API_PERMISSIONS.items() if "ledger:read" in perms}
    assert holders == {"viewer", "api_reader", "recovery_approver", "recovery_admin"}
    assert not any(p.startswith("ledger:") and p != "ledger:read" for perms in API_PERMISSIONS.values() for p in perms)
