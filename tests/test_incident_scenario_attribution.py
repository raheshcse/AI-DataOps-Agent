"""Regression tests: an incident is attributed to ONE scenario, and values fit their columns.

Bug (found on SQL Server, missed by the in-memory fake): when more than one scenario was
active, ``run_pipeline`` wrote the comma-joined list of every active scenario id into
``incident_records.scenario_id`` (VARCHAR(40)) -> "String or binary data would be truncated".
"""

import pytest

from app.db import schema as s
from app.simulation.scenarios import BY_ID, SCENARIOS, scenario_for_failure
from tests.fakes import InMemoryRepo, enforce_column_lengths

PIPELINE_FAILURE = SCENARIOS["pipeline-failure"].scenario_id
DATA_QUALITY = SCENARIOS["data-quality"].scenario_id


def col_len(table, name):
    return table.c[name].type.length


# ------------------------------------------------------------------ the reported failure
def test_incident_with_two_active_scenarios_is_attributed_to_the_cause_only(service, repo):
    service.activate("data-quality")      # left active from an earlier exercise
    service.activate("pipeline-failure")  # the scenario being tested
    result = service.run_pipeline("order_ingestion")  # raised ValueError (truncation) before the fix
    incident = result["incident"]
    assert incident["scenario_id"] == PIPELINE_FAILURE
    assert "," not in incident["scenario_id"]
    # The run still records everything that was active while it ran (column sized for that).
    assert result["run"]["scenario_id"] == f"{PIPELINE_FAILURE},{DATA_QUALITY}"
    assert repo.list_incidents(1)[0]["incident_id"] == incident["incident_id"]


def test_all_six_scenarios_active_never_overflows_any_column(service, repo):
    for key in SCENARIOS:
        service.activate(key)
    for pipeline in ("order_ingestion", "order_validation", "reporting_prep"):
        service.run_pipeline(pipeline)  # would raise on any over-long value
    for incident in repo.list_incidents(50):
        assert incident["scenario_id"] is None or incident["scenario_id"] in BY_ID


def test_failed_save_leaves_no_partial_state(service, repo):
    service.activate("pipeline-failure")
    before = (len(repo.runs), len(repo.incidents), service.clock.now())
    bad = dict(service.run_pipeline("order_ingestion")["incident"], scenario_id="X" * 41)
    with pytest.raises(ValueError, match="truncated"):
        repo.save_run({"run_id": "r", "scenario_id": "ok"}, [], bad)
    assert len(repo.runs) == before[0] + 1 and len(repo.incidents) == before[1] + 1  # only the good run


# --------------------------------------------------------- schema sizing (item 4 of the report)
def test_every_single_scenario_id_fits_the_incident_column():
    limit = col_len(s.incident_records, "scenario_id")
    assert all(len(sid) <= limit for sid in BY_ID), {sid: len(sid) for sid in BY_ID}


def test_run_column_fits_every_scenario_active_at_once():
    joined = ",".join(sorted(BY_ID))
    assert len(joined) <= col_len(s.pipeline_runs, "scenario_id")


def test_incident_scenario_id_is_single_valued_by_design():
    # If this column is ever widened to hold lists, update the model deliberately.
    assert col_len(s.incident_records, "scenario_id") <= 40


# ------------------------------------------------------------ the fake enforces column sizes
def test_fake_rejects_overlong_values_like_sql_server():
    with pytest.raises(ValueError, match="column 'scenario_id'"):
        enforce_column_lengths(s.incident_records, {"scenario_id": "S" * 41})
    enforce_column_lengths(s.incident_records, {"scenario_id": "S" * 40, "extra_key": "ignored"})


# ------------------------------------------------------ failure -> scenario attribution rules
@pytest.mark.parametrize("code,key", [
    ("E1001", "pipeline-failure"), ("E2001", "incomplete-processing"),
    ("E2002", "duplicate-records"), ("E2003", "data-quality"), ("E2004", "data-quality"),
    ("E2005", "schema-mismatch"), ("E2006", "stale-data"),
])
def test_error_code_maps_to_its_scenario_when_active(code, key):
    sid = SCENARIOS[key].scenario_id
    assert scenario_for_failure(code, {sid}) == sid
    assert scenario_for_failure(code, set(BY_ID) - {sid}) is None  # not active -> no attribution


@pytest.mark.parametrize("code", [None, "E9999", "E3001", "E1004", "E-UNKNOWN"])
def test_unmapped_codes_are_not_attributed(code):
    assert scenario_for_failure(code, set(BY_ID)) is None


def test_data_quality_incident_is_attributed_to_data_quality_only(service, repo):
    service.run_pipeline("order_ingestion")
    service.activate("data-quality")
    service.activate("pipeline-failure")
    service.restore("pipeline-failure")  # leave only data-quality active
    result = service.run_pipeline("order_validation")
    assert result["run"]["status"] == "failed"
    assert result["incident"]["scenario_id"] == DATA_QUALITY
