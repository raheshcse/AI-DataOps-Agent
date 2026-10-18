"""Diagnostics: result schema, read-only guarantee, error handling and the roll-up.

These run on the in-memory repository, so they prove the *logic*. That the same queries
work on SQL Server is proven only by tests/integration/test_diagnostics_sqlserver.py.
"""

import copy
import json
from decimal import Decimal
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import OperationalError

from app.db.connection import DatabaseUnavailable
from app.db.repository import SqlRepository
from app.diagnostics import DiagnosticsService, TOOL_NAMES
from app.diagnostics.models import (
    DiagnosticReport,
    DiagnosticResult,
    bounded,
    error_result,
    jsonable,
    worst_severity,
)
from app.diagnostics.readonly import READ_METHODS, ReadOnlyRepo
from app.simulation.ports import SimRepository
from app.simulation.runlog import RunLogStore
from tests.fakes import InMemoryRepo

RESULT_KEYS = {"diagnostic_name", "status", "severity", "summary", "evidence", "affected_records",
               "observed_value", "expected_value", "error_code", "recommended_next_step", "parameters",
               "notes", "evaluated_at"}
WRITE_METHODS = ["seed", "wipe", "save_run", "save_scenario", "insert_orders", "set_sim_now",
                 "delete_scenario_rows"]


def all_results(dx, run_id=None):
    results = [dx.get_pipeline_status(), dx.detect_duplicate_records(), dx.diagnose_data_quality(),
               dx.check_data_freshness(), dx.reconcile_record_counts(), dx.validate_schema_contract()]
    if run_id:
        results.append(dx.inspect_pipeline_logs(run_id))
    return results


# --------------------------------------------------------------------- result schema
def test_every_tool_returns_the_same_structured_schema(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    results = all_results(dx, run_id)
    assert {r.diagnostic_name for r in results} == set(TOOL_NAMES)
    for r in results:
        d = r.to_dict()
        assert set(d) == RESULT_KEYS
        json.dumps(d)  # plain JSON types only
        assert r.status in {"ok", "issue_detected", "warning", "error"}
        assert r.evaluated_at == service.clock.now().isoformat()


def test_result_rejects_unknown_status_or_severity():
    with pytest.raises(ValueError):
        DiagnosticResult("x", "great", "info", "s")
    with pytest.raises(ValueError):
        DiagnosticResult("x", "ok", "catastrophic", "s")


def test_values_are_converted_to_plain_json_types():
    r = DiagnosticResult("x", "ok", "info", "s", evidence=[{"d": Decimal("1.50"), "t": datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                           "day": date(2026, 1, 2), "s": {"b", "a"}}],
                         observed_value={"n": Decimal("2.00")})
    assert r.evidence == [{"d": "1.50", "t": "2026-01-01T00:00:00+00:00", "day": "2026-01-02", "s": ["a", "b"]}]
    assert r.observed_value == {"n": "2.00"}
    assert jsonable((1, 2)) == [1, 2]


def test_bounded_and_worst_severity_helpers():
    items = [str(i) for i in range(120)]
    shown, notes = bounded(items, 50)
    assert len(shown) == 50 and "50 of 120" in notes[0]
    assert bounded(["a"])[1] == []
    assert worst_severity("low", "high", "info") == "high" and worst_severity() == "info"


def test_error_result_shape():
    r = error_result("t", "DIAG-X", "msg", {"a": 1})
    assert (r.status, r.severity, r.error_code, r.parameters) == ("error", "unknown", "DIAG-X", {"a": 1})


def test_scenario_labels_are_not_exposed_in_any_output(dx, service):
    """scenario ids are simulation ground truth; diagnoses must rest on operational evidence."""
    service.run_pipeline("order_ingestion")
    for key in ("data-quality", "duplicate-records", "schema-mismatch", "pipeline-failure"):
        service.activate(key)
    service.run_pipeline("order_ingestion")
    last = service.run_pipeline("order_validation")["run"]["run_id"]
    dump = json.dumps(dx.run_all().to_dict()) + json.dumps(dx.inspect_pipeline_logs(last).to_dict())
    assert "SCN-" not in dump  # no scenario id value anywhere in any result
    # ("scenario_id" may still appear as a *column name* in the feed schema evidence; that is real schema)
    from app.diagnostics.tools._common import run_view
    assert "scenario_id" not in run_view(service.repo.list_runs(limit=1)[0])


# ----------------------------------------------------------------- read-only guarantee
@pytest.mark.parametrize("name", WRITE_METHODS)
def test_read_only_repo_refuses_every_write_method(repo, name):
    with pytest.raises(AttributeError, match="read-only"):
        getattr(ReadOnlyRepo(repo), name)


def test_read_only_repo_cannot_be_modified_and_forwards_reads(repo, service):
    ro = ReadOnlyRepo(repo)
    with pytest.raises(AttributeError):
        ro.insert_orders = lambda *_: 0
    assert ro.table_counts()["orders"] == 173 and ro.get_state()["current_batch_date"] == date(2026, 1, 14)


def test_allow_list_is_only_real_read_methods():
    protocol = {n for n, v in vars(SimRepository).items() if callable(v) and not n.startswith("_")}
    assert READ_METHODS <= protocol
    assert not READ_METHODS & set(WRITE_METHODS)
    for cls in (InMemoryRepo, SqlRepository):  # both implementations provide every protocol method
        assert not [n for n in protocol if not hasattr(cls, n)], cls


def test_running_every_diagnostic_changes_nothing(dx, service, repo, tmp_path):
    service.run_pipeline("order_ingestion")
    service.activate("data-quality")
    files_before = sorted(p.name for p in (tmp_path / "logs").rglob("*.jsonl"))
    before = copy.deepcopy(repo.__dict__)
    report = dx.run_all()
    dx.inspect_pipeline_logs(repo.list_runs(limit=1)[0]["run_id"])
    assert repo.__dict__ == before
    assert sorted(p.name for p in (tmp_path / "logs").rglob("*.jsonl")) == files_before
    assert not any(r.error_code == "DIAG-INTERNAL" for r in report.results)  # no tool needed a blocked method


# --------------------------------------------------------------------- error handling
def test_unseeded_database_gives_a_structured_error_for_every_tool(tmp_path):
    dx = DiagnosticsService(InMemoryRepo(), RunLogStore(tmp_path / "l"))
    for r in all_results(dx, "RUN-X"):
        assert (r.status, r.error_code) == ("error", "DIAG-NOT-SEEDED"), r.diagnostic_name
        assert r.evaluated_at is None


@pytest.mark.parametrize("exc", [
    OperationalError("SELECT 1", {}, Exception("Login failed for user 'sa' on server SECRETHOST\\SQLEXPRESS")),
    DatabaseUnavailable("Could not connect to SQL Server SECRETHOST"),
])
def test_database_failure_is_reported_without_driver_details(dx, repo, monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(repo, "list_runs", boom)
    monkeypatch.setattr(repo, "order_total_mismatches", boom)
    monkeypatch.setattr(repo, "duplicate_order_details", boom)
    for r in [dx.get_pipeline_status(), dx.diagnose_data_quality(), dx.detect_duplicate_records(),
              dx.check_data_freshness()]:
        if r.diagnostic_name == "check_data_freshness":
            continue  # latest_successful_run is not patched; it does not hit the failing call
        assert (r.status, r.error_code) == ("error", "DIAG-DB-ERROR"), r.diagnostic_name
        assert "SECRETHOST" not in json.dumps(r.to_dict()) and "Login failed" not in json.dumps(r.to_dict())
        assert "prereqs" in r.recommended_next_step


def test_unexpected_exception_is_contained_and_not_echoed(dx, repo, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("password=hunter2")
    monkeypatch.setattr(repo, "duplicate_order_details", boom)
    r = dx.detect_duplicate_records()
    assert (r.status, r.error_code) == ("error", "DIAG-INTERNAL")
    assert "RuntimeError" in r.summary and "hunter2" not in json.dumps(r.to_dict())


@pytest.mark.parametrize("call", [
    lambda d: d.get_pipeline_status(limit=0), lambda d: d.get_pipeline_status(limit=101),
    lambda d: d.get_pipeline_status(limit=True), lambda d: d.get_pipeline_status(limit="5"),
    lambda d: d.get_pipeline_status(status_filter="exploded"),
    lambda d: d.get_pipeline_status(pipeline_id="x; DROP TABLE orders"),
    lambda d: d.get_pipeline_status(pipeline_id=7),
    lambda d: d.inspect_pipeline_logs(None), lambda d: d.inspect_pipeline_logs("RUN-X", max_events=0),
    lambda d: d.check_data_freshness(pipeline_id="bad id!"),
    lambda d: d.reconcile_record_counts(batch_date="14/01/2026"),
    lambda d: d.validate_schema_contract(table="orders"),
    lambda d: d.validate_schema_contract(table="stg_order_feed; DROP TABLE orders"),
])
def test_invalid_input_is_a_structured_error_not_an_exception(dx, call):
    r = call(dx)
    assert (r.status, r.error_code) == ("error", "DIAG-INVALID-INPUT")
    assert set(r.to_dict()) == RESULT_KEYS


# ------------------------------------------------------------------------- roll-up
def test_report_on_the_healthy_baseline(dx):
    report = dx.run_all()
    d = report.to_dict()
    json.dumps(d)
    assert [r["diagnostic_name"] for r in d["results"]][:6] == [
        "get_pipeline_status", "check_data_freshness", "reconcile_record_counts",
        "detect_duplicate_records", "diagnose_data_quality", "validate_schema_contract"]
    # the pending 2026-01-14 batch is a (low) warning, nothing else is wrong
    assert report.overall_status == "warning" and report.highest_severity == "low"
    assert d["counts"] == {"ok": 6, "issue_detected": 0, "warning": 1, "error": 0}


def test_report_with_an_issue_and_an_error_status_precedence():
    ok = DiagnosticResult("a", "ok", "info", "s")
    issue = DiagnosticResult("b", "issue_detected", "high", "s")
    err = error_result("c", "DIAG-X", "m")
    assert DiagnosticReport([ok, issue]).overall_status == "issue_detected"
    assert DiagnosticReport([ok, issue, err]).overall_status == "error"
    assert DiagnosticReport([ok]).overall_status == "ok" and DiagnosticReport([]).highest_severity == "info"
    assert DiagnosticReport([issue, err]).highest_severity == "high"  # a diagnostic error has no severity


def test_report_includes_the_latest_run_log_when_runs_exist(dx, service):
    service.activate("pipeline-failure")
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    report = dx.run_all()
    last = report.results[-1]
    assert last.diagnostic_name == "inspect_pipeline_logs" and last.parameters["run_id"] == run_id
    assert report.overall_status == "issue_detected"


def test_cli_exit_codes_and_import():
    from scripts.diagnose import exit_code

    ok = DiagnosticResult("a", "ok", "info", "s")
    assert exit_code([ok]) == 0
    assert exit_code([DiagnosticResult("b", "warning", "low", "s")]) == 0
    assert exit_code([ok, DiagnosticResult("b", "issue_detected", "high", "s")]) == 1
    assert exit_code([ok, error_result("c", "DIAG-X", "m")]) == 2


# ------------------------------------------------------------------- the CLI end to end
@pytest.mark.parametrize("argv,expect_name", [
    (["status"], "get_pipeline_status"),
    (["status", "--pipeline", "order_ingestion", "--status", "failed", "--limit", "5"], "get_pipeline_status"),
    (["duplicates"], "detect_duplicate_records"),
    (["quality"], "diagnose_data_quality"),
    (["freshness"], "check_data_freshness"),
    (["freshness", "--pipeline", "order_ingestion"], "check_data_freshness"),
    (["reconcile"], "reconcile_record_counts"),
    (["reconcile", "--batch-date", "2026-01-14"], "reconcile_record_counts"),
    (["schema"], "validate_schema_contract"),
    (["schema", "--table", "fx_order_feed_drifted"], "validate_schema_contract"),
])
def test_cli_prints_one_json_result_per_tool(dx, monkeypatch, capsys, argv, expect_name):
    import scripts.diagnose as cli

    monkeypatch.setattr(cli, "build_service", lambda: dx)
    monkeypatch.setattr("sys.argv", ["diagnose", *argv])
    code = cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["diagnostic_name"] == expect_name and set(out) == RESULT_KEYS
    assert code == (1 if out["status"] == "issue_detected" else 2 if out["status"] == "error" else 0)


def test_cli_logs_all_and_brief_output(dx, service, monkeypatch, capsys):
    import scripts.diagnose as cli

    service.activate("pipeline-failure")
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    monkeypatch.setattr(cli, "build_service", lambda: dx)

    monkeypatch.setattr("sys.argv", ["diagnose", "logs", run_id, "--max-events", "5"])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "E1001"

    monkeypatch.setattr("sys.argv", ["diagnose", "all"])
    assert cli.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["overall_status"] == "issue_detected" and len(report["results"]) == 7

    monkeypatch.setattr("sys.argv", ["diagnose", "--brief", "all"])
    assert cli.main() == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 7 and lines[0].startswith("[issue_detected/high] get_pipeline_status:")


def test_cli_invalid_input_exits_2_without_a_traceback(dx, monkeypatch, capsys):
    import scripts.diagnose as cli

    monkeypatch.setattr(cli, "build_service", lambda: dx)
    monkeypatch.setattr("sys.argv", ["diagnose", "logs", "../../etc/passwd"])
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "DIAG-INVALID-INPUT"
