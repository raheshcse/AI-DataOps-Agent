"""Behaviour of each diagnostic tool, on the in-memory repository (logic only)."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.diagnostics import DiagnosticsService
from app.simulation.generator import generate_baseline

CURRENT = date(2026, 1, 14)
TS = datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc)


def add_order(repo, event_id, *, customer_id=1, total="10.00", ts=TS, batch=CURRENT, status="paid",
              currency="NZD", item_total=None, unit_price=None, quantity=1):
    unit = Decimal(unit_price if unit_price is not None else item_total if item_total is not None else total)
    line = unit * quantity
    return repo.insert_orders([{
        "customer_id": customer_id, "source_event_id": event_id, "batch_date": batch, "order_ts": ts,
        "status": status, "total_amount": Decimal(total), "currency": currency, "scenario_id": None,
        "items": [{"line_no": 1, "product_id": 1, "quantity": quantity, "unit_price": unit, "line_total": line}]}])


def last_run(service, pipeline):
    return service.repo.list_runs(pipeline, 1)[0]


# ============================================================ Tool 1: pipeline status
def test_status_on_healthy_baseline(dx):
    r = dx.get_pipeline_status()
    assert (r.status, r.severity, r.error_code) == ("ok", "info", None)
    latest = r.evidence[0]["by_pipeline"]
    assert set(latest) == {"order_ingestion", "order_validation", "reporting_prep"}
    assert all(v["status"] == "success" for v in latest.values())
    keys = {"run_id", "pipeline_id", "batch_date", "started_at", "finished_at", "status", "duration_ms",
            "records_expected", "records_processed", "error_code", "error_message"}
    assert keys <= set(latest["order_ingestion"])
    assert r.observed_value["order_ingestion"] == "success"


def test_status_reports_a_failed_latest_run(dx, service):
    service.activate("pipeline-failure")
    run = service.run_pipeline("order_ingestion")["run"]
    r = dx.get_pipeline_status("order_ingestion")
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E1001")
    assert r.evidence[0]["by_pipeline"]["order_ingestion"]["run_id"] == run["run_id"]
    assert r.affected_records == [f"run:{run['run_id']}"] and run["run_id"] in r.recommended_next_step
    assert "timed out" in r.evidence[0]["by_pipeline"]["order_ingestion"]["error_message"]


def test_status_reports_a_warning_run(dx, service):
    service.activate("incomplete-processing")
    service.run_pipeline("order_ingestion")
    r = dx.get_pipeline_status("order_ingestion")
    assert (r.status, r.error_code) == ("warning", "E1004")


def test_status_filter_does_not_change_which_run_is_latest(dx, service):
    failed = dx.get_pipeline_status("order_ingestion", "failed")
    runs = failed.evidence[1]["runs"]
    assert failed.status == "ok"  # latest run is the healthy one, even though the filter shows the old failure
    assert [r["status"] for r in runs] == ["failed"] and runs[0]["error_code"] == "E1002"
    assert failed.evidence[1]["filters"] == {"pipeline_id": "order_ingestion", "status": "failed"}


def test_status_limit_and_empty_history(dx, repo):
    assert len(dx.get_pipeline_status("order_ingestion", limit=3).evidence[1]["runs"]) == 3
    repo.runs = []
    r = dx.get_pipeline_status()
    assert (r.status, r.severity) == ("warning", "low") and "No pipeline runs" in r.summary
    assert dx.get_pipeline_status("order_ingestion", "failed").evidence[1]["runs"] == []


def test_status_unknown_pipeline_lists_the_known_ones(dx):
    r = dx.get_pipeline_status("nonexistent_pipeline")
    assert (r.status, r.error_code) == ("error", "DIAG-UNKNOWN-PIPELINE")
    assert r.evidence[0]["pipeline_ids"] == ["order_ingestion", "order_validation", "reporting_prep"]


# ============================================================ Tool 2: log inspection
def test_logs_of_a_successful_run(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    r = dx.inspect_pipeline_logs(run_id)
    assert (r.status, r.severity) == ("ok", "info")
    summary = r.evidence[0]
    assert summary["type"] == "log_summary" and summary["counts"]["errors"] == 0
    assert summary["log_ref"] == f"pipeline_runs/order_ingestion/{run_id}.jsonl"
    event = r.evidence[-1]["events"][0]
    assert {"timestamp", "severity", "component", "event", "message"} <= set(event)


def test_logs_of_a_failed_run_highlight_errors_and_keep_all_events(dx, service):
    service.activate("pipeline-failure")
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    r = dx.inspect_pipeline_logs(run_id)
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E1001")
    highlights = [e for e in r.evidence if e["type"] == "highlight"]
    assert {h["event"] for h in highlights} == {"feed_connection_timeout", "run_failed"}
    events = next(e for e in r.evidence if e["type"] == "events")["events"]
    assert len(events) == r.evidence[0]["counts"]["events"]  # highlighting removed nothing
    assert [e["line"] for e in events] == sorted(e["line"] for e in events)


def test_logs_of_a_warning_run(dx, service):
    service.activate("incomplete-processing")
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    r = dx.inspect_pipeline_logs(run_id)
    assert (r.status, r.severity, r.error_code) == ("warning", "medium", "E1004")


def test_logs_unknown_run_and_invalid_ids(dx, service, tmp_path):
    r = dx.inspect_pipeline_logs("RUN-DOES-NOT-EXIST")
    assert (r.status, r.error_code) == ("error", "DIAG-UNKNOWN-RUN")
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"message": "TOPSECRET"}\n')
    for bad in ["../../secret", "..", "../secret", "a/b", "a\\b", "", " ", "x" * 100, "*", "RUN-?", "run id",
                "/etc/passwd", "C:\\Windows\\win.ini", "RUN-1\x00"]:
        r = dx.inspect_pipeline_logs(bad)
        assert (r.status, r.error_code) == ("error", "DIAG-INVALID-INPUT"), repr(bad)
        assert "TOPSECRET" not in json.dumps(r.to_dict())


def test_logs_symlink_cannot_escape_the_log_directory(dx, service, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"level": "ERROR", "message": "TOPSECRET", "event": "x"}\n')
    link = service.logs.root / "order_ingestion" / "RUN-LINK.jsonl"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")
    r = dx.inspect_pipeline_logs("RUN-LINK")
    assert r.error_code == "DIAG-UNKNOWN-RUN" and "TOPSECRET" not in json.dumps(r.to_dict())


def test_logs_missing_file_for_an_existing_run(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    service.logs.path_for("order_ingestion", run_id).unlink()
    r = dx.inspect_pipeline_logs(run_id)
    assert (r.status, r.error_code) == ("warning", "DIAG-LOG-MISSING")


def test_logs_malformed_lines_are_reported_without_hiding_good_events(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    path = service.logs.path_for("order_ingestion", run_id)
    good = path.read_text().splitlines()
    path.write_text("\n".join([good[0], "{not json", "[1, 2]", good[1], "", "password=hunter2 {broken"]) + "\n")
    r = dx.inspect_pipeline_logs(run_id)
    assert (r.status, r.severity) == ("warning", "low")
    bad = next(e for e in r.evidence if e["type"] == "malformed_lines")["lines"]
    assert [b["line"] for b in bad] == [2, 3, 6] and bad[1]["reason"] == "not a JSON object"
    assert "hunter2" not in json.dumps(r.to_dict())  # excerpts are redacted
    events = next(e for e in r.evidence if e["type"] == "events")["events"]
    assert [e["line"] for e in events] == [1, 4]


def test_logs_empty_file_is_a_warning(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    service.logs.path_for("order_ingestion", run_id).write_text("")
    r = dx.inspect_pipeline_logs(run_id)
    assert r.status == "warning" and "empty" in r.summary


def test_logs_oversized_file_is_refused(dx, service, monkeypatch):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    monkeypatch.setattr("app.diagnostics.tools._common.MAX_LOG_BYTES", 10)
    assert dx.inspect_pipeline_logs(run_id).error_code == "DIAG-LOG-TOO-LARGE"


def test_logs_secrets_in_messages_are_redacted_and_event_cap_is_noted(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    path = service.logs.path_for("order_ingestion", run_id)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines + ['{"level": "INFO", "message": "key sk-ABCDEFGHIJKLMNOP1234567890", '
                                       '"event": "leak", "component": "x"}']) + "\n")
    r = dx.inspect_pipeline_logs(run_id, max_events=2)
    assert "sk-ABCDEFGHIJKLMNOP" not in json.dumps(r.to_dict())
    assert len(next(e for e in r.evidence if e["type"] == "events")["events"]) == 2
    assert r.notes and "of" in r.notes[0]


def test_logs_that_contradict_the_run_record_are_flagged(dx, service):
    service.activate("pipeline-failure")
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    path = service.logs.path_for("order_ingestion", run_id)
    path.write_text(path.read_text().replace('"run_failed"', '"run_completed"').replace('"ERROR"', '"INFO"'))
    r = dx.inspect_pipeline_logs(run_id)
    assert r.status == "warning" and any(e["type"] == "log_run_mismatch" for e in r.evidence)


# ======================================================= Tool 3: duplicate detection
def test_duplicates_none_on_baseline(dx):
    r = dx.detect_duplicate_records()
    assert (r.status, r.severity, r.observed_value["duplicate_keys"]) == ("ok", "info", 0)
    assert r.evidence[0]["type"] == "key_definition"


def test_duplicates_from_scenario(dx, service):
    service.activate("duplicate-records")
    r = dx.detect_duplicate_records()
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "medium", "E2002")
    groups = [e for e in r.evidence if e["type"] == "duplicate_group"]
    assert len(groups) == 3 and all(g["copies"] == 2 and g["classification"] == "identical_replay" for g in groups)
    assert set(r.affected_records) >= {"order:1", "order:2", "order:3"} and len(r.affected_records) == 6
    assert r.observed_value["extra_order_rows"] == 3 and Decimal(r.observed_value["excess_revenue"]) > 0
    assert all(len(g["order_timestamps"]) == 2 for g in groups)


def test_duplicates_with_conflicting_content_are_more_severe(dx, repo):
    add_order(repo, "EVT-X-1", total="10.00")
    add_order(repo, "EVT-X-1", total="99.00", customer_id=2)
    r = dx.detect_duplicate_records()
    group = next(e for e in r.evidence if e["type"] == "duplicate_group")
    assert group["classification"] == "conflicting_content" and r.severity == "high"
    assert r.observed_value["excess_revenue"] == "99.00"


def test_repeated_business_events_with_distinct_ids_are_not_duplicates(dx, repo):
    for i in range(3):  # same customer, amount, timestamp: legitimate repeat purchases
        add_order(repo, f"EVT-REPEAT-{i}", customer_id=5, total="25.00", ts=TS)
    r = dx.detect_duplicate_records()
    assert r.status == "ok" and r.observed_value["duplicate_keys"] == 0
    assert "separate business events" in r.evidence[0]["not_duplicates"]


def test_duplicate_ids_are_grouped_case_insensitively_like_sql_server(dx, repo):
    add_order(repo, "evt-case-1")
    add_order(repo, "EVT-CASE-1")
    assert dx.detect_duplicate_records().observed_value["duplicate_keys"] == 1


def test_feed_repeats_loaded_vs_pending(dx, service, repo):
    feed = repo.get_feed_rows(CURRENT)
    repo.feed.append(dict(feed[0], feed_id=9001))  # same event id, still not loaded into orders
    pending = dx.detect_duplicate_records()
    assert (pending.status, pending.severity) == ("warning", "low")
    rep = next(e for e in pending.evidence if e["type"] == "feed_repeat")
    assert rep["already_loaded_into_orders"] is False and "more than once" in rep["note"]
    assert pending.affected_records == ["feed:" + str(feed[0]["feed_id"]), "feed:9001"]
    service.run_pipeline("order_ingestion")  # ingestion loads it (and, as noted, inserts both rows)
    loaded = dx.detect_duplicate_records()
    assert loaded.status == "issue_detected"  # the repeat did create duplicate orders: surfaced, not hidden


def test_feed_repeat_of_an_already_loaded_event_is_informational(dx, repo):
    old = repo.feed[0]
    repo.feed.append(dict(old, feed_id=9002))
    r = dx.detect_duplicate_records()
    assert r.status == "ok" and "already loaded" in r.summary
    assert next(e for e in r.evidence if e["type"] == "feed_repeat")["already_loaded_into_orders"] is True


# ========================================================= Tool 4: data quality
def test_quality_healthy(dx):
    r = dx.diagnose_data_quality()
    assert (r.status, r.severity) == ("ok", "info")
    rules = [e for e in r.evidence if e["type"] == "rule_result"]
    assert [x["rule_name"] for x in rules] == [
        "order_total_matches_line_items", "order_timestamp_not_in_future", "no_duplicate_source_events",
        "duplicate_driven_overcount", "required_values_present", "amounts_non_negative", "order_status_valid"]
    assert all(x["outcome"] == "pass" for x in rules)


def test_quality_scenario_total_mismatch_and_future_timestamp(dx, service):
    service.activate("data-quality")
    r = dx.diagnose_data_quality()
    assert (r.status, r.error_code) == ("issue_detected", "E2003")
    rules = {e["rule_name"]: e for e in r.evidence if e["type"] == "rule_result"}
    totals, future = rules["order_total_matches_line_items"], rules["order_timestamp_not_in_future"]
    assert (totals["outcome"], totals["severity"]) == ("fail", "medium")
    assert (future["outcome"], future["severity"]) == ("warn", "low")  # warning, distinct from the failure
    assert len(totals["affected_records"]) == 3 and len(future["affected_records"]) == 1
    row = totals["rows"][0]
    assert Decimal(row["difference"]) == Decimal(row["total_amount"]) - Decimal(row["items_total"]) != 0
    assert totals["observed_value"] == {"orders_with_mismatch": 3}
    assert totals["expected_condition"] == "order total equals the sum of its line items"
    assert r.observed_value == {"failed_rules": ["order_total_matches_line_items"],
                                "warned_rules": ["order_timestamp_not_in_future"]}


def test_quality_warning_only(dx, repo):
    add_order(repo, "EVT-FUTURE", ts=datetime(2026, 2, 1, tzinfo=timezone.utc))
    r = dx.diagnose_data_quality()
    assert (r.status, r.severity, r.error_code) == ("warning", "low", "E2004")


def test_quality_duplicates_and_overcount(dx, service):
    service.activate("duplicate-records")
    r = dx.diagnose_data_quality()
    rules = {e["rule_name"]: e for e in r.evidence if e["type"] == "rule_result"}
    assert rules["no_duplicate_source_events"]["outcome"] == "fail"
    over = rules["duplicate_driven_overcount"]
    assert over["outcome"] == "fail" and over["observed_value"]["surplus_order_rows"] == 3
    assert len(over["affected_records"]) == 3  # only the surplus copies


def test_quality_rules_the_schema_cannot_express(dx, repo):
    add_order(repo, "   ", currency="NZD")  # blank required value
    add_order(repo, "EVT-NEG", total="10.00", unit_price="-5.00")  # negative price, total != items
    add_order(repo, "EVT-STATUS", status="teleported")  # status outside the domain
    r = dx.diagnose_data_quality()
    rules = {e["rule_name"]: e for e in r.evidence if e["type"] == "rule_result"}
    assert rules["required_values_present"]["outcome"] == "fail"
    assert rules["amounts_non_negative"]["affected_records"][0].startswith("order_item:")
    assert rules["order_status_valid"]["observed_value"] == {"orders_with_invalid_status": 1}
    assert r.status == "issue_detected" and len(r.observed_value["failed_rules"]) >= 3


def test_quality_never_modifies_records_and_bounds_large_results(dx, repo):
    for i in range(60):
        add_order(repo, f"EVT-BAD-{i}", total="10.00", item_total="9.00")
    before = [dict(o) for o in repo.orders]
    r = dx.diagnose_data_quality()
    assert repo.orders == before
    totals = next(e for e in r.evidence if e.get("rule_name") == "order_total_matches_line_items")
    assert len(totals["affected_records"]) == 50 and totals["notes"] and len(r.affected_records) == 50
    assert r.notes and len(totals["rows"]) == 20


# ======================================================== Tool 5: data freshness
def test_freshness_fresh_on_baseline_uses_simulated_clock(dx, service):
    r = dx.check_data_freshness()
    assert (r.status, r.error_code) == ("ok", None)
    ev = r.evidence[0]
    assert ev["age_minutes"] == 1380.0 and ev["threshold_minutes"] == 1500 and ev["stale"] is False
    assert ev["reference_time"] == service.clock.now().isoformat() == r.evaluated_at
    assert "simulated clock" in ev["reference_source"] and ev["margin_minutes"] == 120.0
    assert r.to_dict() == dx.check_data_freshness().to_dict()  # deterministic


def test_freshness_stale_scenario(dx, service):
    service.run_pipeline("order_ingestion")
    service.activate("stale-data")
    r = dx.check_data_freshness()
    ev = r.evidence[0]
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E2006")
    assert ev["stale"] and ev["minutes_over_threshold"] == ev["age_minutes"] - 1500 > 0
    assert ev["last_successful_run"]["pipeline_id"] == "order_ingestion"
    assert r.observed_value["age_minutes"] == ev["age_minutes"]


@pytest.mark.parametrize("threshold,expected_status", [(1380, "ok"), (1379, "issue_detected")])
def test_freshness_boundary_is_stale_only_when_strictly_older(service, threshold, expected_status):
    d = DiagnosticsService(service.repo, service.logs, threshold)
    assert d.check_data_freshness().status == expected_status


def test_freshness_explains_runs_since_the_last_success(dx, service):
    service.activate("pipeline-failure")
    failed = service.run_pipeline("order_ingestion")["run"]["run_id"]
    since = dx.check_data_freshness().evidence[1]
    assert since["count"] == 1 and since["runs"][0]["run_id"] == failed and since["runs"][0]["status"] == "failed"


def test_freshness_without_any_successful_run(dx, repo):
    repo.runs = [r for r in repo.runs if r["status"] != "success"]
    r = dx.check_data_freshness()
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E2006")
    assert r.observed_value == "no successful run"


@pytest.mark.parametrize("mutate,reason", [
    (lambda run, now: run.update(finished_at=None), "missing or has no timezone"),
    (lambda run, now: run.update(finished_at=datetime(2026, 1, 14, 1, 0)), "missing or has no timezone"),
    (lambda run, now: run.update(finished_at=now + timedelta(hours=1)), "after the reference time"),
])
def test_freshness_invalid_timestamps_are_an_error_not_a_guess(dx, repo, mutate, reason):
    run = next(r for r in repo.runs if r["pipeline_id"] == "order_ingestion" and r["status"] == "success")
    for r in repo.runs:  # make this the only/latest success so it is the one examined
        if r is not run and r["pipeline_id"] == "order_ingestion" and r["status"] == "success":
            r["status"] = "failed"
    mutate(run, repo.state["sim_now"])
    r = dx.check_data_freshness()
    assert (r.status, r.error_code) == ("error", "DIAG-INVALID-TIMESTAMP") and reason in r.summary


def test_freshness_unknown_pipeline(dx):
    assert dx.check_data_freshness("nope").error_code == "DIAG-UNKNOWN-PIPELINE"


# ================================================ Tool 6: record-count reconciliation
def comparison(r, name):
    return next(e for e in r.evidence if e.get("name") == name)


def test_reconcile_pending_batch_is_not_called_a_failure(dx):
    r = dx.reconcile_record_counts()
    assert (r.status, r.severity, r.error_code) == ("warning", "low", None)
    assert "has not been ingested" in r.summary and r.observed_value["missing_from_orders"] == 30
    assert comparison(r, "rejected_records")["outcome"] == "not_available"


def test_reconcile_after_clean_ingestion(dx, service):
    service.run_pipeline("order_ingestion")
    r = dx.reconcile_record_counts()
    assert (r.status, r.error_code) == ("ok", None) and r.affected_records == []
    assert comparison(r, "feed_rows_vs_run_expected")["outcome"] == "match"
    assert comparison(r, "run_expected_vs_processed")["outcome"] == "match"
    assert comparison(r, "processed_vs_inserted_plus_skipped")["right"]["value"] == 30
    assert comparison(r, "feed_events_vs_orders_present")["difference"] == 0
    assert comparison(r, "orders_in_batch_vs_feed_events")["outcome"] == "match"


def test_reconcile_partial_load(dx, service):
    service.activate("incomplete-processing")
    service.run_pipeline("order_ingestion")
    r = dx.reconcile_record_counts()
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E1004")
    short = comparison(r, "run_expected_vs_processed")
    assert (short["outcome"], short["difference"], short["right"]["value"]) == ("mismatch", 12, 18)
    assert comparison(r, "feed_events_vs_orders_present")["difference"] == 12
    assert len(r.affected_records) == 12 and r.observed_value["missing_from_orders"] == 12


def test_reconcile_failed_run(dx, service):
    service.activate("pipeline-failure")
    service.run_pipeline("order_ingestion")
    r = dx.reconcile_record_counts()
    assert (r.status, r.error_code) == ("issue_detected", "E1001") and r.observed_value["missing_from_orders"] == 30


def test_reconcile_idempotent_reruns_are_not_failures(dx, service, repo):
    service.run_pipeline("order_ingestion")
    service.run_pipeline("order_ingestion")  # second run: every row already present -> 30 skipped
    r = dx.reconcile_record_counts()
    assert r.status == "ok" and "30 skipped as already loaded" in r.summary
    assert comparison(r, "processed_vs_inserted_plus_skipped")["outcome"] == "match"
    assert next(e for e in r.evidence if e["type"] == "idempotent_skips")["skipped_existing"] == 30
    assert repo.table_counts()["orders"] == 203


def test_reconcile_extra_orders_are_a_warning_pointing_at_duplicates(dx, service, repo):
    service.run_pipeline("order_ingestion")
    event = repo.get_feed_rows(CURRENT)[0]["source_event_id"]
    add_order(repo, event)
    r = dx.reconcile_record_counts()
    assert (r.status, r.severity) == ("warning", "medium")
    assert comparison(r, "orders_in_batch_vs_feed_events")["difference"] == 1
    assert "detect_duplicate_records" in r.recommended_next_step


def test_reconcile_works_without_the_run_log(dx, service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    service.logs.path_for("order_ingestion", run_id).unlink()
    r = dx.reconcile_record_counts()
    assert r.status == "ok" and comparison(r, "processed_vs_inserted_plus_skipped")["outcome"] == "not_available"


def test_reconcile_batch_date_inputs(dx, service):
    service.run_pipeline("order_ingestion")
    assert dx.reconcile_record_counts("2026-01-14").to_dict()["parameters"]["batch_date"] == "2026-01-14"
    assert dx.reconcile_record_counts(CURRENT).status == "ok"
    empty = dx.reconcile_record_counts("2030-01-01")
    assert (empty.status, empty.severity) == ("warning", "low") and "nothing to reconcile" in empty.summary
    older = dx.reconcile_record_counts("2026-01-13")  # a historical batch with an earlier run
    assert older.status in {"ok", "warning"} and older.evidence[0]["feed_rows"] > 0


# ============================================ Tool 7: schema-contract validation
def test_schema_healthy_contract(dx):
    r = dx.validate_schema_contract()
    assert (r.status, r.error_code) == ("ok", None)
    ev = r.evidence[0]
    assert ev["checked_table"] == "stg_order_feed" and ev["expected_schema"] == ev["observed_schema"]
    assert r.observed_value == {"columns": 9, "missing": 0, "unexpected": 0, "type_mismatches": 0}


def test_schema_drift_via_the_default_table(dx, service):
    service.activate("schema-mismatch")
    r = dx.validate_schema_contract()
    ev = r.evidence[0]
    assert (r.status, r.severity, r.error_code) == ("issue_detected", "high", "E2005")
    assert ev["checked_table"] == "fx_order_feed_drifted" and "order_validation inspects" in ev["table_selection"]
    assert ev["missing_columns"] == [{"column": "currency", "expected_type": "string"}]
    assert ev["unexpected_columns"] == [{"column": "legacy_flag", "observed_type": "bool"}]
    assert ev["type_mismatches"] == [{"column": "order_total", "expected_type": "decimal", "observed_type": "string"}]
    assert sorted(r.affected_records) == ["column:currency", "column:legacy_flag", "column:order_total"]
    assert r.observed_value == {"columns": 9, "missing": 1, "unexpected": 1, "type_mismatches": 1}
    assert r.expected_value["columns"] == 9


def test_schema_explicit_table_overrides_the_default(dx, service):
    fixture = dx.validate_schema_contract("fx_order_feed_drifted")
    assert fixture.status == "issue_detected" and fixture.evidence[0]["table_selection"] == "explicit"
    service.activate("schema-mismatch")
    assert dx.validate_schema_contract("stg_order_feed").status == "ok"  # the real table is untouched


def test_schema_tool_reads_metadata_only(dx, repo):
    before = repo.table_counts()
    dx.validate_schema_contract()
    assert repo.table_counts() == before


# ================================================= cross-cutting: shared building blocks
def test_run_log_store_locate(service):
    run_id = service.run_pipeline("order_ingestion")["run"]["run_id"]
    assert service.logs.locate(run_id).name == f"{run_id}.jsonl"
    assert service.logs.locate("RUN-NOPE") is None
    with pytest.raises(ValueError):
        service.logs.locate("../x")


def test_feed_table_for_matches_what_order_validation_uses():
    from app.simulation.pipelines import feed_table_for
    from app.simulation.scenarios import SCENARIOS

    assert feed_table_for(set()) == "stg_order_feed"
    assert feed_table_for({SCENARIOS["schema-mismatch"].scenario_id}) == "fx_order_feed_drifted"


def test_order_status_constant_matches_the_database_constraint():
    from app.db import schema as s

    check = next(c for c in s.orders.constraints if "status_valid" in str(getattr(c, "name", "")))
    assert all(f"'{status}'" in str(check.sqltext) for status in s.ORDER_STATUSES)
    assert len(s.ORDER_STATUSES) == str(check.sqltext).count("'") // 2


def test_list_runs_status_filter_on_the_fake(repo, service):
    assert {r["status"] for r in repo.list_runs(status="failed", limit=50)} == {"failed"}
    assert generate_baseline().expected_counts()["pipeline_runs"] == 22
