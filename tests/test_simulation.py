"""Pipeline simulator, structured logs and the six failure scenarios.

These run against the in-memory repository, so they prove the *logic*. That the same
behaviour holds in SQL Server is proven only by tests/integration (not run here).
"""

import json

import pytest

from app.simulation.clock import SimulationNotSeeded
from app.simulation.generator import generate_baseline
from app.simulation.pipelines import PIPELINE_FUNCTIONS
from app.simulation.runlog import RunLogStore
from app.simulation.scenarios import SCENARIOS
from app.simulation.service import (
    ScenarioRestoreRefused,
    SeedConflict,
    SimulationService,
    UnknownPipeline,
    UnknownScenario,
)
from tests.fakes import InMemoryRepo


def run(svc, pipeline):
    return svc.run_pipeline(pipeline)


def validations_by_rule(result):
    return {v["rule_name"]: v for v in result["validations"]}


def clean_ingest(svc):
    r = run(svc, "order_ingestion")
    assert r["run"]["status"] == "success"
    return r


# ----------------------------------------------------------------- seeding
def test_seed_is_idempotent(service, repo):
    before = repo.table_counts()
    assert "Already seeded" in service.seed()
    assert repo.table_counts() == before == generate_baseline().expected_counts()


def test_seed_refuses_unknown_existing_data(tmp_path):
    repo = InMemoryRepo()
    repo.customers.append({"customer_id": 1})  # data but no state row
    svc = SimulationService(repo, RunLogStore(tmp_path))
    with pytest.raises(SeedConflict):
        svc.seed()


def test_seed_refuses_different_seed(service, repo, tmp_path):
    other = SimulationService(repo, RunLogStore(tmp_path), seed=999)
    with pytest.raises(SeedConflict):
        other.seed()


def test_seed_writes_history_logs(service, tmp_path):
    files = list((tmp_path / "logs" / "pipeline_runs").glob("*/*.jsonl"))
    assert len(files) == 22


def test_fresh_baseline_verifies_and_normal_setup_activates_no_scenario(service):
    report = service.verify()
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]
    assert service.active_scenario_ids() == set()
    assert all(s["status"] == "inactive" for s in service.scenario_status())


def test_unseeded_database_is_reported(tmp_path):
    svc = SimulationService(InMemoryRepo(), RunLogStore(tmp_path))
    with pytest.raises(SimulationNotSeeded):
        svc.run_pipeline("order_ingestion")


def test_unknown_pipeline_and_scenario(service):
    with pytest.raises(UnknownPipeline):
        service.run_pipeline("nope")
    with pytest.raises(UnknownScenario):
        service.activate("nope")
    with pytest.raises(UnknownScenario):
        service.restore("nope")


# ------------------------------------------------------------- normal pipelines
def test_successful_run_metadata_and_effect(service, repo):
    before = repo.table_counts()
    r = clean_ingest(service)
    run_row = r["run"]
    assert (run_row["records_expected"], run_row["records_processed"]) == (30, 30)
    assert run_row["error_code"] is None and run_row["scenario_id"] is None
    assert run_row["duration_ms"] == 1500 + 12 * 30
    assert (run_row["finished_at"] - run_row["started_at"]).total_seconds() == 1.86
    after = repo.table_counts()
    assert after["orders"] == before["orders"] + 30 and after["pipeline_runs"] == before["pipeline_runs"] + 1
    assert repo.get_run(run_row["run_id"])["status"] == "success"


def test_ingestion_is_idempotent(service, repo):
    clean_ingest(service)
    orders = repo.table_counts()["orders"]
    second = run(service, "order_ingestion")["run"]
    assert second["status"] == "success" and second["records_processed"] == 30
    assert repo.table_counts()["orders"] == orders  # nothing loaded twice
    assert repo.duplicate_events() == []


def test_full_clean_sequence_passes_every_rule(service):
    clean_ingest(service)
    val = run(service, "order_validation")
    assert val["run"]["status"] == "success" and val["incident"] is None
    assert {v["outcome"] for v in val["validations"]} == {"pass"} and len(val["validations"]) == 6
    rpt = run(service, "reporting_prep")["run"]
    assert (rpt["status"], rpt["records_expected"], rpt["records_processed"]) == ("success", 30, 30)
    assert len({r["run_id"] for r in service.repo.list_runs(limit=100)}) == 25  # unique run ids


def test_clock_advances_deterministically_between_runs(service):
    a = clean_ingest(service)["run"]
    b = run(service, "order_validation")["run"]
    assert b["started_at"] == a["finished_at"] + __import__("datetime").timedelta(seconds=60)


def test_reporting_before_ingestion_fails_with_incident(service):
    r = run(service, "reporting_prep")
    assert r["run"]["status"] == "failed" and r["run"]["error_code"] == "E3001"
    assert r["incident"]["status"] == "open" and r["incident"]["run_id"] == r["run"]["run_id"]


def test_validation_before_ingestion_flags_the_unloaded_batch(service):
    r = run(service, "order_validation")
    rules = validations_by_rule(r)
    assert r["run"]["status"] == "failed" and r["run"]["error_code"] == "E2001"
    assert rules["batch_completeness"]["observed_value"] == "0" and rules["batch_completeness"]["expected_value"] == "30"


# --------------------------------------------------------------------- logs
def final_event(svc, result):
    return svc.logs.read(result["run"]["run_id"])[-1]


def test_logs_correspond_to_run_status(service):
    service.activate("pipeline-failure")
    failed = run(service, "order_ingestion")
    events = service.logs.read(failed["run"]["run_id"])
    assert events[-1]["event"] == "run_failed" and events[-1]["level"] == "ERROR"
    assert not any(e["event"] == "run_completed" for e in events)
    service.restore("pipeline-failure")
    ok = clean_ingest(service)
    assert final_event(service, ok)["event"] == "run_completed"
    assert not any(e["level"] == "ERROR" for e in service.logs.read(ok["run"]["run_id"]))


def test_log_events_have_the_documented_fields_and_correlation_ids(service):
    r = clean_ingest(service)
    for e in service.logs.read(r["run"]["run_id"]):
        assert set(e) == {"timestamp", "level", "component", "pipeline_id", "run_id", "event",
                          "message", "error_code", "scenario_id", "data"}
        assert e["run_id"] == r["run"]["run_id"] and e["pipeline_id"] == "order_ingestion"
    first, last = service.logs.read(r["run"]["run_id"])[0], service.logs.read(r["run"]["run_id"])[-1]
    assert first["timestamp"].startswith("2026-01-15T01:00:00") and last["timestamp"] > first["timestamp"]


def test_unexpected_exception_is_recorded_without_leaking_its_text(service, monkeypatch):
    def boom(ctx):
        raise ValueError("connection string password=hunter2")

    monkeypatch.setitem(PIPELINE_FUNCTIONS, "order_ingestion", boom)
    r = run(service, "order_ingestion")
    assert r["run"]["status"] == "failed" and r["run"]["error_code"] == "E9999"
    text = json.dumps(service.logs.read(r["run"]["run_id"])) + str(r["run"]) + str(r["incident"])
    assert "hunter2" not in text and "ValueError" in r["run"]["error_message"]


# --------------------------------------------------- scenario 1: pipeline failure
def test_scenario_1_pipeline_failure(service, repo):
    result = service.activate("pipeline-failure")
    assert result.changed and service.scenario_status()[0]["status"] == "active"
    r = run(service, "order_ingestion")["run"]
    assert (r["status"], r["error_code"]) == ("failed", "E1001")
    assert "timed out" in r["error_message"] and r["scenario_id"] == "SCN-01-PIPELINE-FAILURE"
    assert repo.table_counts()["orders"] == 173  # nothing loaded
    incident = repo.list_incidents(1)[0]
    assert (incident["category"], incident["severity"], incident["status"]) == ("pipeline_failure", "high", "open")
    assert incident["run_id"] == r["run_id"] and incident["evidence_ref"].endswith(f"{r['run_id']}.jsonl")
    error_log = service.logs.read(r["run_id"])
    assert any(e["error_code"] == "E1001" and e["level"] == "ERROR" for e in error_log)
    service.restore("pipeline-failure")
    assert run(service, "order_ingestion")["run"]["status"] == "success"


# --------------------------------------------------- scenario 2: data quality
def test_scenario_2_data_quality(service, repo):
    clean_ingest(service)
    result = service.activate("data-quality")
    assert result.changed and repo.tagged_row_counts()["orders"] == 4
    val = run(service, "order_validation")
    rules = validations_by_rule(val)
    assert val["run"]["status"] == "failed" and val["run"]["error_code"] == "E2003"
    totals = rules["order_total_matches_line_items"]
    assert totals["outcome"] == "fail" and totals["observed_value"] == "3"
    assert "SCN2-0001" in totals["record_ref"] and "violated" not in totals["record_ref"]
    assert rules["order_timestamp_not_in_future"]["outcome"] == "warn"
    assert rules["batch_completeness"]["outcome"] == "pass"
    assert val["incident"]["category"] == "data_quality"
    assert any(e["event"] == "validation_rule_evaluated" and e["level"] == "ERROR"
               and e["data"]["rule"] == "order_total_matches_line_items"
               for e in service.logs.read(val["run"]["run_id"]))
    assert "Removed 4" in service.restore("data-quality").message
    assert repo.tagged_row_counts()["orders"] == 0 and repo.order_total_mismatches() == []
    assert run(service, "order_validation")["run"]["status"] == "success"


# ------------------------------------------------- scenario 3: duplicate records
def test_scenario_3_duplicate_records(service, repo):
    clean_ingest(service)
    baseline_orders = repo.table_counts()["orders"]
    service.activate("duplicate-records")
    assert repo.table_counts()["orders"] == baseline_orders + 3
    dups = repo.duplicate_events()
    assert len(dups) == 3 and all(d["copies"] == 2 for d in dups)
    val = run(service, "order_validation")
    res = validations_by_rule(val)["no_duplicate_source_events"]
    assert val["run"]["error_code"] == "E2002" and res["outcome"] == "fail"
    assert "x2" in res["record_ref"] and res["observed_value"] == "3"
    assert val["incident"]["category"] == "duplicate_records"
    details = json.loads(repo.get_scenarios()["SCN-03-DUPLICATE-RECORDS"]["details_json"])
    assert details["injected_orders"] == 3 and len(details["duplicated_event_ids"]) == 3
    service.restore("duplicate-records")
    assert repo.duplicate_events() == [] and repo.table_counts()["orders"] == baseline_orders


# ------------------------------------------------------- scenario 4: stale data
def test_scenario_4_stale_data(service):
    clean_ingest(service)  # a freshly loaded batch is fresh...
    assert service.freshness()["stale"] is False
    service.activate("stale-data")  # ...until 30 simulated hours pass with no new load
    f = service.freshness()
    assert f["stale"] is True and f["threshold_minutes"] == 1500
    assert f["age_minutes"] > 1500 and f["last_success_at"].isoformat().startswith("2026-01-15T01:00:01")
    val = run(service, "order_validation")
    fresh = validations_by_rule(val)["data_freshness"]
    assert fresh["outcome"] == "fail" and "threshold 1500" in fresh["detail"]
    assert val["run"]["error_code"] == "E2006" and val["incident"]["category"] == "stale_data"


def test_scenario_4_on_a_fresh_baseline_uses_the_last_scheduled_load(service):
    assert service.freshness()["stale"] is False
    service.activate("stale-data")
    f = service.freshness()
    assert f["stale"] is True and f["last_success_at"].isoformat().startswith("2026-01-14T02:00:01")


def test_scenario_4_restore_before_any_run_restores_the_clock(service):
    before = service.clock.now()
    service.activate("stale-data")
    assert service.clock.now() > before
    service.restore("stale-data")
    assert service.clock.now() == before and service.freshness()["stale"] is False


def test_scenario_4_restore_refused_after_runs_and_recovery_by_fresh_ingestion(service):
    service.activate("stale-data")
    clean_ingest(service)
    assert service.freshness()["stale"] is False  # a successful load is the real recovery
    with pytest.raises(ScenarioRestoreRefused):
        service.restore("stale-data")


# ------------------------------------------------ scenario 5: incomplete processing
def test_scenario_5_incomplete_processing(service, repo):
    service.activate("incomplete-processing")
    ing = run(service, "order_ingestion")["run"]
    assert (ing["status"], ing["error_code"]) == ("warning", "E1004")
    assert (ing["records_expected"], ing["records_processed"]) == (30, 18)
    assert repo.table_counts()["orders"] == 173 + 18
    log = service.logs.read(ing["run_id"])
    assert log[-1]["event"] == "run_completed_with_warnings" and log[-1]["level"] == "WARNING"
    assert any(e["event"] == "incomplete_load" for e in log)
    val = run(service, "order_validation")
    comp = validations_by_rule(val)["batch_completeness"]
    assert (comp["outcome"], comp["expected_value"], comp["observed_value"]) == ("fail", "30", "18")
    assert "shortfall 12" in comp["detail"] and val["run"]["error_code"] == "E2001"
    assert val["incident"]["category"] == "incomplete_processing"
    assert run(service, "reporting_prep")["run"]["status"] == "warning"
    service.restore("incomplete-processing")
    rerun = run(service, "order_ingestion")["run"]  # manual re-run loads only the missing rows
    assert rerun["status"] == "success" and repo.table_counts()["orders"] == 173 + 30
    assert run(service, "order_validation")["run"]["status"] == "success"


# ---------------------------------------------------- scenario 6: schema mismatch
def test_scenario_6_schema_mismatch(service, repo):
    clean_ingest(service)
    service.activate("schema-mismatch")
    val = run(service, "order_validation")
    res = validations_by_rule(val)["feed_schema_contract"]
    assert val["run"]["error_code"] == "E2005" and res["outcome"] == "fail"
    assert res["record_ref"] == "fx_order_feed_drifted"
    assert "missing: currency" in res["detail"] and "unexpected: legacy_flag" in res["detail"]
    assert "order_total: expected decimal, observed string" in res["detail"]
    assert res["expected_value"] == "9 columns" and res["observed_value"] == "9 columns"
    assert val["incident"]["category"] == "schema_mismatch"
    assert repo.observed_schema("stg_order_feed")  # the real feed table was never altered
    service.restore("schema-mismatch")
    assert validations_by_rule(run(service, "order_validation"))["feed_schema_contract"]["outcome"] == "pass"


# --------------------------------------------- activation / restoration semantics
@pytest.mark.parametrize("key", list(SCENARIOS))
def test_repeated_activation_is_a_noop_and_restore_is_defined(service, repo, key):
    first = service.activate(key)
    snapshot = (repo.table_counts(), service.clock.now())
    second = service.activate(key)
    assert first.changed and not second.changed and "Already active" in second.message
    assert (repo.table_counts(), service.clock.now()) == snapshot
    assert service.restore(key).changed
    assert not service.restore(key).changed  # restoring twice is harmless
    assert next(s for s in service.scenario_status() if s["key"] == key)["status"] == "restored"
    again = service.activate(key)  # re-activation after restore works
    assert again.changed
    service.restore(key)


def test_restore_all_returns_to_a_verifiable_scenario_free_state(service):
    for key in ("pipeline-failure", "data-quality", "duplicate-records", "schema-mismatch", "incomplete-processing"):
        service.activate(key)
    assert len(service.restore_all()) == 5
    assert service.active_scenario_ids() == set()
    report = service.verify(strict=False)
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


def test_scenario_states_are_distinguishable_from_baseline(service, repo):
    service.activate("data-quality")
    report = service.verify()
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert not report["ok"] and "no failure scenario active" in failed and "no scenario-tagged rows" in failed


# ------------------------------------------------------------------ baseline reset
def test_reset_restores_the_clean_baseline(service, repo, tmp_path):
    clean_ingest(service)
    service.activate("data-quality")
    run(service, "order_validation")
    assert not service.verify()["ok"]
    message = service.reset()
    assert "Deleted" in message and "Seeded baseline" in message
    assert service.verify()["ok"] and service.active_scenario_ids() == set()
    assert len(list((tmp_path / "logs" / "pipeline_runs").glob("*/*.jsonl"))) == 22  # only history logs
    assert service.clock.now() == generate_baseline().state["sim_now"]
