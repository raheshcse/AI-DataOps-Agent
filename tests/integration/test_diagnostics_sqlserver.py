"""SQL Server integration tests for the Phase 3 diagnostic tools.

Same safety model as the Phase 2 integration tests: only the isolated ``*_test`` database
is touched (the ``svc`` fixture wipes and re-seeds it per test), never ``dataops_sim``.
They prove what the in-memory fake cannot: the real T-SQL, driver data types and metadata.
Skipped unless RUN_SQLSERVER_TESTS=1; a skip is not a pass.
"""

import json
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import make_engine
from app.db.repository import SqlRepository
from app.diagnostics import DiagnosticsService
from app.simulation.runlog import RunLogStore


def by_name(report):
    return {r.diagnostic_name: r for r in report.results}


def fingerprint(svc):
    return (svc.repo.table_counts(), svc.repo.get_state()["sim_now"],
            {k: v["status"] for k, v in svc.repo.get_scenarios().items()},
            [r["run_id"] for r in svc.repo.list_runs(limit=100)])


def comparison(result, name):
    return next(e for e in result.evidence if e.get("name") == name)


def test_diagnostics_run_in_the_isolated_test_database(svc, engine, sql_settings):
    assert sql_settings.db_name.lower().endswith("_test") and sql_settings.db_auth_mode == "windows"
    with engine.connect() as c:
        assert c.execute(text("SELECT DB_NAME()")).scalar_one() == sql_settings.db_name


def test_every_tool_on_the_seeded_baseline(dx):
    report = dx.run_all()
    json.dumps(report.to_dict())
    results = by_name(report)
    assert [r.diagnostic_name for r in report.results if r.status == "error"] == []
    assert results["get_pipeline_status"].status == "ok"
    ingestion = results["get_pipeline_status"].evidence[0]["by_pipeline"]["order_ingestion"]
    assert ingestion["finished_at"].endswith("+00:00") and ingestion["records_expected"] is not None  # UTC round trip
    assert results["check_data_freshness"].evidence[0]["age_minutes"] == 1380.0
    recon = results["reconcile_record_counts"]
    assert recon.status == "warning" and recon.observed_value["missing_from_orders"] == 30
    for name in ("detect_duplicate_records", "diagnose_data_quality", "validate_schema_contract"):
        assert results[name].status == "ok", name


def test_pipeline_failure_is_visible_in_status_logs_and_reconciliation(dx, svc):
    svc.activate("pipeline-failure")
    run_id = svc.run_pipeline("order_ingestion")["run"]["run_id"]
    status = dx.get_pipeline_status("order_ingestion")
    assert (status.status, status.error_code) == ("issue_detected", "E1001")
    log = dx.inspect_pipeline_logs(run_id)
    assert log.status == "issue_detected" and log.error_code == "E1001"
    assert comparison(dx.reconcile_record_counts(), "feed_events_vs_orders_present")["difference"] == 30
    assert dx.reconcile_record_counts().status == "issue_detected"


def test_incomplete_processing_is_quantified(dx, svc):
    svc.activate("incomplete-processing")
    svc.run_pipeline("order_ingestion")
    recon = dx.reconcile_record_counts()
    assert (recon.status, recon.error_code) == ("issue_detected", "E1004")
    assert comparison(recon, "run_expected_vs_processed")["difference"] == 12
    assert len(recon.affected_records) == 12


def test_data_quality_findings_with_real_decimals(dx, svc):
    svc.run_pipeline("order_ingestion")
    svc.activate("data-quality")
    r = dx.diagnose_data_quality()
    rules = {e["rule_name"]: e for e in r.evidence if e["type"] == "rule_result"}
    totals = rules["order_total_matches_line_items"]
    assert (r.status, r.error_code) == ("issue_detected", "E2003") and totals["outcome"] == "fail"
    assert len(totals["affected_records"]) == 3
    for row in totals["rows"]:  # SQL DECIMAL arrives as an exact value, not a float
        assert Decimal(row["total_amount"]) - Decimal(row["items_total"]) == Decimal(row["difference"]) != 0
    assert rules["order_timestamp_not_in_future"]["outcome"] == "warn"


def test_duplicate_detection_and_overcount_on_sql_server(dx, svc):
    svc.run_pipeline("order_ingestion")
    svc.activate("duplicate-records")
    r = dx.detect_duplicate_records()
    groups = [e for e in r.evidence if e["type"] == "duplicate_group"]
    assert (r.status, r.error_code) == ("issue_detected", "E2002") and len(groups) == 3
    assert all(g["copies"] == 2 and len(g["order_ids"]) == 2 for g in groups)
    over = next(e for e in dx.diagnose_data_quality().evidence if e.get("rule_name") == "duplicate_driven_overcount")
    assert over["outcome"] == "fail" and over["observed_value"]["surplus_order_rows"] == 3


def test_quality_scan_finds_what_only_the_data_can_contain(dx, svc):
    """Blank ids and negative prices are legal for the schema; invalid status/negative total are not."""
    svc.repo.insert_orders([
        {"customer_id": 1, "source_event_id": "   ", "batch_date": svc.repo.get_state()["current_batch_date"],
         "order_ts": svc.clock.now(), "status": "paid", "total_amount": Decimal("10.00"), "currency": "NZD",
         "scenario_id": None, "items": [{"line_no": 1, "product_id": 1, "quantity": 1,
                                          "unit_price": Decimal("-5.00"), "line_total": Decimal("-5.00")}]}])
    with pytest.raises(SQLAlchemyError):  # the database itself refuses an invalid status
        svc.repo.insert_orders([{"customer_id": 1, "source_event_id": "EVT-BADSTATUS", "batch_date":
                                 svc.repo.get_state()["current_batch_date"], "order_ts": svc.clock.now(),
                                 "status": "teleported", "total_amount": Decimal("1.00"), "currency": "NZD",
                                 "scenario_id": None, "items": []}])
    rules = {e["rule_name"]: e for e in dx.diagnose_data_quality().evidence if e["type"] == "rule_result"}
    assert rules["required_values_present"]["outcome"] == "fail"
    assert rules["amounts_non_negative"]["outcome"] == "fail"
    assert rules["amounts_non_negative"]["affected_records"][0].startswith("order_item:")
    assert rules["order_status_valid"]["outcome"] == "pass"


def test_stale_data_freshness_uses_the_simulated_clock(dx, svc):
    svc.run_pipeline("order_ingestion")
    svc.activate("stale-data")
    r = dx.check_data_freshness()
    expected_age = round((svc.clock.now() - svc.repo.latest_successful_run("order_ingestion")["finished_at"])
                         .total_seconds() / 60, 1)
    assert (r.status, r.error_code) == ("issue_detected", "E2006")
    assert r.evidence[0]["age_minutes"] == expected_age > 1500
    assert r.evaluated_at == svc.clock.now().isoformat()


def test_schema_contract_against_real_database_metadata(dx, svc):
    assert dx.validate_schema_contract().status == "ok"  # the real feed table matches the contract
    svc.run_pipeline("order_ingestion")
    svc.activate("schema-mismatch")
    r = dx.validate_schema_contract()
    ev = r.evidence[0]
    assert (r.status, r.error_code, ev["checked_table"]) == ("issue_detected", "E2005", "fx_order_feed_drifted")
    assert [m["column"] for m in ev["missing_columns"]] == ["currency"]
    assert [u["column"] for u in ev["unexpected_columns"]] == ["legacy_flag"]
    assert ev["type_mismatches"] == [{"column": "order_total", "expected_type": "decimal", "observed_type": "string"}]


def test_diagnostics_do_not_modify_the_database(dx, svc):
    svc.activate("pipeline-failure")
    svc.run_pipeline("order_ingestion")
    before = fingerprint(svc)
    dx.run_all()
    dx.inspect_pipeline_logs(before[3][0])
    dx.reconcile_record_counts("2026-01-13")
    assert fingerprint(svc) == before


def test_unseeded_database_is_reported_not_raised(dx, svc):
    svc.repo.wipe()  # the isolated test database only; the next test re-seeds
    r = dx.get_pipeline_status()
    assert (r.status, r.error_code) == ("error", "DIAG-NOT-SEEDED")


def test_connection_failure_does_not_leak_server_details(sql_settings, svc):
    broken = make_engine(sql_settings, database="dataops_sim_no_such_test", admin=True)
    try:
        r = DiagnosticsService(SqlRepository(broken), RunLogStore(svc.logs.root.parent)).get_pipeline_status()
    finally:
        broken.dispose()
    assert (r.status, r.error_code) == ("error", "DIAG-DB-ERROR")
    dump = json.dumps(r.to_dict())
    assert sql_settings.db_server not in dump and "Login failed" not in dump and "Cannot open database" not in dump
