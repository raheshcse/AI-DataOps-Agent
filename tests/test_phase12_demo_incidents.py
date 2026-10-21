"""Phase 12: synthetic demonstration incidents for the simulator (in-memory repository; no SQL Server involved).

These tests prove the seeding LOGIC (distinct, deterministic, additive, idempotent, non-destructive). They do not
prove behaviour against a real SQL Server instance: that needs the manual command in the README."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from app.simulation import demo_incidents as di
from app.simulation.demo_incidents import build_catalogue, seed_demo_incidents
from tests.api_support import api  # noqa: F401

SEVERITIES = {"low", "medium", "high", "critical"}


def test_catalogue_is_98_distinct_deterministic_and_covers_every_requested_category():
    a, b = build_catalogue(), build_catalogue()
    assert len(a) == 98
    assert [i.run["run_id"] for i in a] == [i.run["run_id"] for i in b] and [i.title for i in a] == [i.title for i in b]
    assert len({i.run["run_id"] for i in a}) == 98 and len({i.title for i in a}) == 98
    assert {i.category for i in a} == {c.key for c in di.CATEGORIES} and len(di.CATEGORIES) == 12
    assert {i.severity for i in a} == SEVERITIES
    assert {i.status for i in a} == {"open", "investigating", "resolved"}
    assert all(i.run["error_code"].startswith("DEMO-") for i in a)
    assert all(i.run["scenario_id"] is None for i in a)  # never tagged as a scenario, so scenario restore cannot remove them
    assert all("Synthetic demonstration incident" in i.description for i in a)
    assert all(i.run["run_id"].startswith(di.DEMO_RUN_PREFIX) for i in a)
    assert all(i.run["started_at"] < di.ANCHOR_NOW - di.timedelta(days=7) for i in a)  # before the baseline window


def test_seeding_adds_98_keeps_existing_rows_and_is_idempotent(api):
    e = api()
    sim = e.env.sim
    before = sim.repo.list_incidents(100000)
    snapshot = copy.deepcopy(before)
    first = seed_demo_incidents(sim.repo, sim.logs)
    assert (first.existing_before, first.inserted, first.skipped_duplicates, first.final_total) == (len(before), 98, 0, len(before) + 98)
    after = sim.repo.list_incidents(100000)
    assert len(after) == len(before) + 98 and len({i["incident_id"] for i in after}) == len(after)
    kept = {i["incident_id"]: i for i in after}
    assert all(kept[i["incident_id"]] == i for i in snapshot)  # the pre-existing records are byte-for-byte unchanged
    second = seed_demo_incidents(sim.repo, sim.logs)
    assert (second.inserted, second.skipped_duplicates, second.final_total) == (0, 98, len(before) + 98)
    assert len(sim.repo.list_incidents(100000)) == len(before) + 98
    assert sim.repo.integrity_problems() == []
    assert sim.repo.tagged_row_counts()["incident_records"] == 0


def test_dry_run_writes_nothing(api):
    e = api()
    sim = e.env.sim
    n = len(sim.repo.list_incidents(100000))
    r = seed_demo_incidents(sim.repo, sim.logs, dry_run=True)
    assert r.inserted == 98 and r.dry_run and r.final_total == n
    assert len(sim.repo.list_incidents(100000)) == n and sim.repo.demo_row_counts()["pipeline_runs"] == 0


def test_every_incident_has_a_run_validations_and_a_readable_log(api):
    e = api()
    sim = e.env.sim
    seed_demo_incidents(sim.repo, sim.logs)
    demo = [i for i in sim.repo.list_incidents(100000) if str(i["run_id"]).startswith(di.DEMO_RUN_PREFIX)]
    assert len(demo) == 98
    for inc in demo:
        run = sim.repo.get_run(inc["run_id"])
        assert run and run["status"] == "failed" and run["error_code"] and run["pipeline_id"] == inc["pipeline_id"]
        events = sim.logs.read(inc["run_id"])
        assert events and any(ev["level"] == "ERROR" for ev in events) and any(ev["data"].get("synthetic") for ev in events)
    with_rules = [i for i in demo if sim.repo.get_validations(i["run_id"])]
    assert len(with_rules) >= 40 and all(v["outcome"] == "fail" for i in with_rules for v in sim.repo.get_validations(i["run_id"]))


def test_strict_verification_reports_demo_rows_separately_not_as_drift(api):
    e = api()
    sim = e.env.sim
    failing_before = {c["check"] for c in sim.verify(strict=True)["checks"] if not c["ok"]}  # the fixture's own state
    seed_demo_incidents(sim.repo, sim.logs)
    result = sim.verify(strict=True)
    failing_after = {c["check"] for c in result["checks"] if not c["ok"]}
    assert failing_after == failing_before, (failing_before, failing_after)  # seeding adds no failure of its own
    assert not [c for c in failing_after if "row count" in c and ("pipeline_runs" in c or "incident_records" in c or "validation_results" in c)]
    assert any("synthetic demo" in c["check"] and "98" in c["detail"] for c in result["checks"])
    assert any("synthetic demo rows" in c["detail"] for c in result["checks"])  # visibly separated, not hidden


def test_latest_successful_run_is_unchanged_by_seeding(api):
    e = api()
    sim = e.env.sim
    before = {p["pipeline_id"]: sim.repo.latest_successful_run(p["pipeline_id"]) for p in sim.repo.list_pipelines()}
    seed_demo_incidents(sim.repo, sim.logs)
    assert {p: sim.repo.latest_successful_run(p) for p in before} == before


def test_api_lists_all_incidents_with_pagination_filters_and_search(api):
    e = api()
    base = e.get("/simulator/incidents", "reader").json()["total"]
    seed_demo_incidents(e.env.sim.repo, e.env.sim.logs)
    full = e.get("/simulator/incidents?limit=200", "reader").json()
    assert full["total"] == base + 98 > 50 and len(full["items"]) == full["total"]
    p1 = e.get("/simulator/incidents?limit=25&offset=0", "reader").json()
    p2 = e.get("/simulator/incidents?limit=25&offset=25", "reader").json()
    assert p1["total"] == p2["total"] == full["total"] and not ({i["incident_id"] for i in p1["items"]} & {i["incident_id"] for i in p2["items"]})
    crit = e.get("/simulator/incidents?severity=critical&limit=200", "reader").json()
    assert crit["total"] > 0 and all(i["severity"] == "critical" for i in crit["items"])
    opened = e.get("/simulator/incidents?status=open&severity=high&limit=200", "reader").json()
    assert opened["total"] > 0 and all(i["status"] == "open" and i["severity"] == "high" for i in opened["items"])
    ing = e.get("/simulator/incidents?q=ingestion_failure&limit=200", "reader").json()
    assert ing["total"] == 10 and all(i["category"] == "ingestion_failure" for i in ing["items"])
    run_id = full["items"][0]["run_id"]
    assert e.get(f"/simulator/incidents?q={run_id}", "reader").json()["total"] == 1
    assert e.get("/simulator/incidents?q=zzz-no-match", "reader").json()["total"] == 0
    assert not any(i["imported"] for i in full["items"])  # nothing is imported automatically


def test_a_seeded_incident_can_be_imported_idempotently_and_investigated(api):
    e = api()
    sim = e.env.sim
    seed_demo_incidents(sim.repo, sim.logs)
    target = e.get("/simulator/incidents?severity=high&q=source_timeout&limit=50", "reader").json()["items"][0]
    source_before = copy.deepcopy([i for i in sim.repo.list_incidents(100000) if i["incident_id"] == target["incident_id"]])
    one = e.post(f"/simulator/incidents/{target['incident_id']}/import", "operator")
    two = e.post(f"/simulator/incidents/{target['incident_id']}/import", "operator")
    assert one.status_code == 201 and two.status_code == 200 and not two.json()["created"]
    wid = one.json()["incident"]["incident_id"]
    assert two.json()["incident"]["incident_id"] == wid
    assert e.get("/simulator/incidents?imported=true", "reader").json()["total"] == 1
    assert [i for i in sim.repo.list_incidents(100000) if i["incident_id"] == target["incident_id"]] == source_before  # source untouched
    done = e.investigate(wid)
    assert done["status"] in ("completed", "failed") and done["incident_id"] == wid
    if done["status"] == "completed":
        assert done["report"] is not None and done["report_status"]  # a stored, verified report; its conclusion is not asserted
    assert e.env.executor.count == 0 and e.get(f"/incidents/{wid}/recoveries", "reader").json() == []
    # synthetic codes never match the real scenario codes, so no recovery is proposed for a synthetic incident
    refused = e.request_recovery(wid)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "no_recovery_suggested"
    assert e.env.executor.count == 0


def test_seeder_module_contains_no_destructive_statements():
    text = Path(di.__file__).read_text(encoding="utf-8") + (Path(__file__).resolve().parents[1] / "scripts" / "db_seed_demo_incidents.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", text)
    for banned in (r"\bDELETE\b", r"\bTRUNCATE\b", r"\bDROP\b", r"\bUPDATE\b", r"\.wipe\(", r"\.delete", r"\.clear\("):
        assert not re.search(banned, code, re.I), banned
