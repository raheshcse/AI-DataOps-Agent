"""Orchestration of the simulated environment: seed, run, scenarios, verify.

Pure orchestration over a ``SimRepository``; contains no SQL. Scripts and API
endpoints call this layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.simulation.clock import ANCHOR_NOW, SimClock, SimulationNotSeeded
from app.simulation.events import (
    build_run_events,
    failure_steps,
    make_incident,
    make_run_id,
    make_run_row,
)
from app.simulation.generator import BASELINE_VERSION, generate_baseline
from app.simulation.pipelines import PIPELINE_FUNCTIONS, Context, Outcome, default_duration_ms
from app.simulation.ports import SimRepository
from app.simulation.runlog import RunLogStore
from app.simulation.scenarios import (
    BY_ID,
    SCENARIOS,
    STALE_ADVANCE,
    build_bad_orders,
    build_duplicate_orders,
    scenario_for_failure,
)
from app.simulation.validators import RULE_ERROR_CODES, RULE_INCIDENT, calc_freshness


class UnknownPipeline(Exception):
    pass


class UnknownScenario(Exception):
    pass


class SeedConflict(Exception):
    """The database already holds data that this seed would conflict with."""


class ScenarioRestoreRefused(Exception):
    pass


@dataclass
class ScenarioResult:
    key: str
    scenario_id: str
    changed: bool
    message: str


class SimulationService:
    def __init__(self, repo: SimRepository, log_store: RunLogStore, *,
                 seed: int = 20260115, freshness_threshold_minutes: int = 1500):
        self.repo = repo
        self.logs = log_store
        self.seed_value = seed
        self.threshold = freshness_threshold_minutes
        self.clock = SimClock(repo)

    # ------------------------------------------------------------------ seeding
    def seed(self) -> str:
        """Idempotent baseline seed. Returns a human-readable outcome."""
        state = self.repo.get_state()
        if state is not None:
            if state["baseline_version"] == BASELINE_VERSION and state["seed"] == self.seed_value:
                return "Already seeded with this baseline version and seed; no changes made."
            raise SeedConflict("The database was seeded with a different baseline version or seed. "
                               "Use the guarded reset to rebuild it.")
        if not self.repo.is_empty():
            raise SeedConflict("Tables contain data but there is no simulation state row. "
                               "Refusing to seed on top of unknown data; use the guarded reset.")
        data = generate_baseline(self.seed_value, self.threshold)
        self.repo.seed(data)
        for run in data.runs:
            self.logs.write(run["pipeline_id"], run["run_id"], data.run_events[run["run_id"]])
        counts = data.expected_counts()
        return "Seeded baseline: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v)

    def reset(self) -> str:
        """Delete ALL simulation rows and generated logs, then re-seed. Callers must
        have passed the safety guard (see ``app.db.safety``)."""
        deleted = self.repo.wipe()
        removed_logs = self.logs.clear()
        message = self.seed()
        return (f"Deleted {sum(deleted.values())} rows and {removed_logs} log files. {message}")

    # ----------------------------------------------------------------- pipelines
    def active_scenario_ids(self) -> set[str]:
        return {sid for sid, row in self.repo.get_scenarios().items() if row["status"] == "active"}

    def run_pipeline(self, pipeline_id: str, batch_date: date | None = None) -> dict[str, Any]:
        state = self.repo.get_state()
        if state is None:
            raise SimulationNotSeeded("The simulation database has not been seeded.")
        if self.repo.get_pipeline(pipeline_id) is None or pipeline_id not in PIPELINE_FUNCTIONS:
            raise UnknownPipeline(f"Unknown pipeline: {pipeline_id}")
        batch = batch_date or state["current_batch_date"]
        now = state["sim_now"]
        active = self.active_scenario_ids()

        ctx = Context(self.repo, batch, now, active, self.threshold)
        try:
            outcome = PIPELINE_FUNCTIONS[pipeline_id](ctx)
        except Exception as exc:  # recorded as a failed run; details stay out of the record
            msg = f"Unexpected internal error ({type(exc).__name__})"
            outcome = Outcome("failed", None, None, "E9999", msg,
                              failure_steps("pipeline_runner", "unexpected_error", "E9999", msg))

        duration = outcome.duration_ms or default_duration_ms(pipeline_id, outcome.processed)
        run_id = make_run_id(pipeline_id, now)
        n = 1
        while self.repo.run_exists(run_id):
            n += 1
            run_id = f"{make_run_id(pipeline_id, now)}-{n}"
        row = make_run_row(
            run_id=run_id, pipeline_id=pipeline_id, batch_date=batch, started_at=now,
            duration_ms=duration, status=outcome.status, expected=outcome.expected,
            processed=outcome.processed, error_code=outcome.error_code,
            error_message=outcome.error_message, scenario_id=",".join(sorted(active)) or None)
        evidence = self.logs.relative_ref(pipeline_id, run_id)
        validations = [v.to_row(run_id, row["finished_at"]) for v in outcome.validations]
        incident = None
        if outcome.status == "failed":
            incident = self._incident_for(row, outcome, evidence, active)
        self.repo.save_run(row, validations, incident)
        events = build_run_events(row, outcome.steps, outcome.validations)
        self.logs.write(pipeline_id, run_id, events)
        self.clock.advance_to(row["finished_at"] + timedelta(seconds=60))
        return {"run": row, "validations": validations, "incident": incident, "log_ref": evidence}

    def _incident_for(self, run: dict, outcome: Outcome, evidence: str, active: set[str]) -> dict:
        failed = [v for v in outcome.validations if v.outcome == "fail"]
        if failed:
            category, severity = RULE_INCIDENT[failed[0].rule_name]
            cause_code = RULE_ERROR_CODES[failed[0].rule_name]
        else:
            category, severity = "pipeline_failure", "high"
            cause_code = run["error_code"]
        return make_incident(
            incident_id=self.repo.next_incident_id(), category=category, severity=severity,
            title=f"{run['pipeline_id']} failed for batch {run['batch_date']}",
            description=(run["error_message"] or "Pipeline failed")[:900], status="open",
            created_at=run["finished_at"], run=run, evidence_ref=evidence,
            scenario_id=scenario_for_failure(cause_code, active))

    # ----------------------------------------------------------------- scenarios
    def scenario_status(self) -> list[dict[str, Any]]:
        states = self.repo.get_scenarios()
        out = []
        for s in SCENARIOS.values():
            row = states.get(s.scenario_id)
            out.append({"key": s.key, "scenario_id": s.scenario_id, "title": s.title,
                        "mechanism": s.mechanism, "how_to_observe": s.how_to_observe,
                        "status": row["status"] if row else "inactive",
                        "activated_at": row["activated_at"] if row else None,
                        "details": json.loads(row["details_json"]) if row else None})
        return out

    def activate(self, key: str) -> ScenarioResult:
        sc = SCENARIOS.get(key)
        if sc is None:
            raise UnknownScenario(f"Unknown scenario: {key}")
        current = self.repo.get_scenarios().get(sc.scenario_id)
        if current and current["status"] == "active":
            return ScenarioResult(key, sc.scenario_id, False, "Already active; nothing changed.")
        now = self.clock.now()
        details: dict[str, Any] = {"mechanism": sc.mechanism}
        activated_at = now
        if key == "data-quality":
            bad = build_bad_orders(self.repo.sample_orders(4), now, sc.scenario_id)
            details["injected_orders"] = self.repo.insert_orders(bad)
            details["injected_event_ids"] = [o["source_event_id"] for o in bad]
        elif key == "duplicate-records":
            dups = build_duplicate_orders(self.repo.sample_orders(3), sc.scenario_id)
            details["injected_orders"] = self.repo.insert_orders(dups)
            details["duplicated_event_ids"] = [o["source_event_id"] for o in dups]
        elif key == "stale-data":
            advanced = now + STALE_ADVANCE
            details.update(previous_sim_now=now.isoformat(), advanced_to=advanced.isoformat(),
                           hours=STALE_ADVANCE.total_seconds() / 3600)
            self.clock.advance_to(advanced)
        self.repo.save_scenario({"scenario_id": sc.scenario_id, "status": "active",
                                 "activated_at": activated_at, "restored_at": None,
                                 "details_json": json.dumps(details, sort_keys=True, default=str)})
        return ScenarioResult(key, sc.scenario_id, True, f"Activated. {sc.mechanism}")

    def restore(self, key: str) -> ScenarioResult:
        sc = SCENARIOS.get(key)
        if sc is None:
            raise UnknownScenario(f"Unknown scenario: {key}")
        row = self.repo.get_scenarios().get(sc.scenario_id)
        if not row or row["status"] != "active":
            return ScenarioResult(key, sc.scenario_id, False, "Not active; nothing to restore.")
        details = json.loads(row["details_json"])
        message = "Condition removed."
        if key in ("data-quality", "duplicate-records"):
            message = f"Removed {self.repo.delete_scenario_rows(sc.scenario_id)} injected order(s)."
        elif key == "stale-data":
            newest = self.repo.list_runs(limit=1)
            if newest and newest[0]["started_at"] > row["activated_at"]:
                raise ScenarioRestoreRefused(
                    "Pipeline runs were recorded while stale-data was active, so moving the "
                    "clock back would make run history inconsistent. Use the guarded baseline "
                    "reset (scripts.db_reset) instead.")
            self.clock.advance_to(datetime.fromisoformat(details["previous_sim_now"]))
            message = "Simulated clock restored to its pre-scenario value."
        self.repo.save_scenario({"scenario_id": sc.scenario_id, "status": "restored",
                                 "activated_at": row["activated_at"], "restored_at": self.clock.now(),
                                 "details_json": row["details_json"]})
        return ScenarioResult(key, sc.scenario_id, True, message)

    def restore_all(self) -> list[ScenarioResult]:
        results = []
        for s in SCENARIOS.values():
            if s.scenario_id in self.active_scenario_ids():
                results.append(self.restore(s.key))
        return results

    # ------------------------------------------------------------- diagnostics
    def freshness(self, pipeline_id: str = "order_ingestion") -> dict[str, Any]:
        last = self.repo.latest_successful_run(pipeline_id)
        result = calc_freshness(last["finished_at"] if last else None, self.clock.now(), self.threshold)
        result["pipeline_id"] = pipeline_id
        result["evaluated_at"] = self.clock.now()
        return result

    def verify(self, strict: bool = True) -> dict[str, Any]:
        """Integrity checks always; baseline-equality checks when ``strict``."""
        checks: list[dict[str, Any]] = []

        def add(group: str, name: str, ok: bool, detail: str) -> None:
            checks.append({"group": group, "check": name, "ok": ok, "detail": detail})

        state = self.repo.get_state()
        add("integrity", "simulation state row present", state is not None,
            "found" if state else "missing: run the seed script")
        counts = self.repo.table_counts()
        problems = self.repo.integrity_problems()
        add("integrity", "relationships (no orphan rows)", not problems, "; ".join(problems) or "no orphans")
        active = self.active_scenario_ids()
        add("integrity", "no failure scenario active", not active, ", ".join(sorted(active)) or "none")
        tagged = {k: v for k, v in self.repo.tagged_row_counts().items() if v}
        add("integrity", "no scenario-tagged rows", not tagged, str(tagged) if tagged else "none")
        if strict:
            expected = generate_baseline(self.seed_value, self.threshold).expected_counts()
            for table, want in expected.items():
                got = counts.get(table)
                add("baseline", f"row count {table}", got == want, f"expected {want}, found {got}")
            add("baseline", "simulated clock at baseline anchor",
                bool(state) and state["sim_now"] == ANCHOR_NOW,
                f"found {state['sim_now'].isoformat() if state else None}")
        return {"ok": all(c["ok"] for c in checks), "checks": checks}
