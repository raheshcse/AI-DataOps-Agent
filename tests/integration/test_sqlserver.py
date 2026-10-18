"""Real SQL Server checks of the same behaviour the unit tests prove in memory."""

import pytest
from sqlalchemy import inspect, text

from app.db.schema import TABLE_NAMES
from app.simulation.generator import generate_baseline


def test_schema_exists_with_all_tables(engine):
    assert set(TABLE_NAMES) <= set(inspect(engine).get_table_names())


def test_seed_matches_documented_counts_and_verifies(svc):
    assert svc.repo.table_counts() == generate_baseline().expected_counts()
    report = svc.verify()
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


def test_seed_is_idempotent_in_sql_server(svc):
    before = svc.repo.table_counts()
    assert "Already seeded" in svc.seed()
    assert svc.repo.table_counts() == before


def test_foreign_keys_are_enforced_by_the_database(svc, engine):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("INSERT INTO orders (order_id, customer_id, source_event_id, batch_date, order_ts, "
                       "status, total_amount, currency) VALUES (999999, 424242, 'X', '2026-01-01', "
                       "'2026-01-01', 'paid', 1, 'NZD')"))


def test_check_constraint_rejects_bad_status(svc, engine):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("UPDATE orders SET status = 'bogus' WHERE order_id = 1"))


def test_observed_schema_of_feed_and_drifted_fixture(svc):
    from app.simulation.schema_contract import ORDER_FEED_CONTRACT

    assert svc.repo.observed_schema("stg_order_feed") == ORDER_FEED_CONTRACT
    drifted = svc.repo.observed_schema("fx_order_feed_drifted")
    assert "currency" not in drifted and drifted["legacy_flag"] == "bool" and drifted["order_total"] == "string"


def test_timestamps_roundtrip_as_utc(svc):
    run = svc.repo.list_runs(limit=1)[0]
    assert run["started_at"].tzinfo is not None and run["started_at"].utcoffset().total_seconds() == 0


def test_clean_pipeline_sequence(svc):
    assert svc.run_pipeline("order_ingestion")["run"]["status"] == "success"
    assert svc.run_pipeline("order_validation")["run"]["status"] == "success"
    assert svc.run_pipeline("reporting_prep")["run"]["status"] == "success"
    assert svc.repo.table_counts()["orders"] == 203


EXPECTED = {
    # key: (setup pipelines, pipeline to run, expected run status, expected error code)
    "pipeline-failure": ([], "order_ingestion", "failed", "E1001"),
    "data-quality": (["order_ingestion"], "order_validation", "failed", "E2003"),
    "duplicate-records": (["order_ingestion"], "order_validation", "failed", "E2002"),
    "stale-data": (["order_ingestion"], "order_validation", "failed", "E2006"),
    "incomplete-processing": ([], "order_ingestion", "warning", "E1004"),
    "schema-mismatch": (["order_ingestion"], "order_validation", "failed", "E2005"),
}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_each_scenario_produces_its_evidence_and_restores(svc, key):
    setup, pipeline, status, code = EXPECTED[key]
    for p in setup:
        assert svc.run_pipeline(p)["run"]["status"] == "success"
    assert svc.activate(key).changed
    assert not svc.activate(key).changed  # repeated activation is a no-op
    result = svc.run_pipeline(pipeline)
    assert (result["run"]["status"], result["run"]["error_code"]) == (status, code)
    assert svc.logs.read(result["run"]["run_id"])  # log written and readable
    persisted = svc.repo.get_run(result["run"]["run_id"])
    assert persisted["status"] == status
    if status == "failed":
        assert svc.repo.list_incidents(1)[0]["run_id"] == result["run"]["run_id"]
    try:
        svc.restore(key)
    except Exception:  # stale-data restore is refused after runs; reset is the documented path
        pass
    svc.reset()
    assert svc.verify()["ok"]


def test_incident_with_two_active_scenarios_fits_its_columns_in_sql_server(svc):
    """Regression: a comma-joined scenario list used to overflow incident_records.scenario_id
    (VARCHAR(40)) -> 'String or binary data would be truncated'. Only SQL Server enforces this."""
    svc.activate("data-quality")
    svc.activate("pipeline-failure")
    result = svc.run_pipeline("order_ingestion")
    assert result["incident"]["scenario_id"] == "SCN-01-PIPELINE-FAILURE"
    stored = svc.repo.list_incidents(1)[0]
    assert stored["scenario_id"] == "SCN-01-PIPELINE-FAILURE"
    assert svc.repo.get_run(result["run"]["run_id"])["scenario_id"] == \
        "SCN-01-PIPELINE-FAILURE,SCN-02-DATA-QUALITY"


def test_all_six_scenarios_active_at_once_can_be_recorded_in_sql_server(svc):
    from app.simulation.scenarios import SCENARIOS
    for key in SCENARIOS:
        svc.activate(key)
    for pipeline in ("order_ingestion", "order_validation", "reporting_prep"):
        svc.run_pipeline(pipeline)  # must not raise a truncation error
