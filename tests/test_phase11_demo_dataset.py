"""The 100-incident synthetic demo dataset: count, uniqueness, variety, constraints, determinism and isolation."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from app.demo import DEMO_SOURCE, generate_demo_incidents, validate_demo_incidents
from app.demo.incidents import INCIDENT_TYPES, PIPELINES, SEVERITIES, STATUSES

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "pipeline_execution_failure", "missing_source_records", "duplicate_records", "stale_delayed_data", "schema_mismatch",
    "incomplete_batch", "failed_api_ingestion", "malformed_records", "null_required_fields", "unexpected_row_count_change",
    "referential_integrity", "transformation_failure", "source_to_staging_mismatch", "staging_to_target_mismatch",
    "freshness_sla_breach", "authentication_permission_failure", "upstream_dependency_outage", "rate_limit_timeout",
    "data_quality_check_failure", "suspicious_change"}
REQUIRED_FIELDS = {"incident_id", "title", "description", "category", "severity", "status", "affected_pipeline", "detected_at", "run_ref",
                   "expected_value", "observed_value", "evidence", "probable_cause", "recommended_next_step", "human_review_required",
                   "source", "synthetic"}


def test_exactly_100_unique_valid_incidents():
    items = generate_demo_incidents()
    assert len(items) == 100 and validate_demo_incidents(items) == []
    assert len({i["incident_id"] for i in items}) == 100 and len({i["run_ref"] for i in items}) == 100
    assert len({i["title"] for i in items}) == 100 and len({i["description"] for i in items}) == 100


def test_all_twenty_categories_with_five_each_and_varied_attributes():
    items = generate_demo_incidents()
    by_cat = Counter(i["category"] for i in items)
    assert set(by_cat) == CATEGORIES and set(by_cat.values()) == {5}
    assert set(Counter(i["severity"] for i in items)) == set(SEVERITIES)
    assert set(Counter(i["status"] for i in items)) == set(STATUSES)
    assert len({i["affected_pipeline"] for i in items}) >= 6 and len({i["detected_at"][:10] for i in items}) > 40
    assert {i["incident_type"] for i in items} <= set(INCIDENT_TYPES)


def test_every_record_has_the_required_fields_and_is_labelled_synthetic():
    for i in generate_demo_incidents():
        assert REQUIRED_FIELDS <= set(i)
        assert i["source"] == DEMO_SOURCE and i["synthetic"] is True and i["incident_id"].startswith("DEMO-")
        assert i["evidence"] and i["evidence"][0]["label"].startswith("Synthetic")
        assert i["probable_cause_status"] == "hypothesis_unconfirmed" and i["affected_pipeline"] in PIPELINES
        assert not i["incident_id"].startswith(("INC-", "inc-"))  # cannot collide with simulator or workflow ids


def test_generation_is_deterministic():
    assert generate_demo_incidents() == generate_demo_incidents()


def test_validator_catches_corruption():
    items = generate_demo_incidents()
    assert validate_demo_incidents(items[:-1])
    broken = [dict(i) for i in items]
    broken[0]["incident_id"] = broken[1]["incident_id"]
    broken[2]["severity"] = "catastrophic"
    problems = validate_demo_incidents(broken)
    assert any("duplicate incident ids" in p for p in problems) and any("invalid enumeration" in p for p in problems)


def test_demo_package_cannot_touch_any_real_store():
    """Isolation by construction: the package imports no database, persistence, governance or network module."""
    forbidden = ("sqlalchemy", "sqlite3", "pyodbc", "httpx", "requests", "app.db", "app.persistence", "app.governance", "app.recovery", "socket")
    for path in (ROOT / "app" / "demo").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert not n.startswith(forbidden), f"{path.name} imports {n}"


def test_main_stores_are_unchanged_by_using_the_demo_api(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    sim_before = e.env.sim.repo.list_incidents(1000)
    store_before = e.store.counts()
    ledger_before = e.env.recorder.ledger.store.path.read_bytes() if e.env.recorder.ledger.store.path.exists() else b""
    for path in ("/demo/summary", "/demo/incidents?limit=100", "/demo/incidents/DEMO-0050", "/dashboard/summary"):
        assert e.get(path, "reader").status_code == 200
    assert e.env.sim.repo.list_incidents(1000) == sim_before and e.store.counts() == store_before
    assert (e.env.recorder.ledger.store.path.read_bytes() if e.env.recorder.ledger.store.path.exists() else b"") == ledger_before
    assert e.get(f"/incidents/{iid}", "reader").status_code == 200


from tests.api_support import api  # noqa: E402,F401
