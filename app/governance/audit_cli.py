"""Audit the governance ledger: hash-chain integrity, then the five VSL semantic checks.

    python -m app.governance.audit_cli [--ledger PATH] [--max-monitor-gap-seconds N] [--json]

Exit codes: 0 = integrity and all five checks pass; 1 = integrity or any semantic check failed;
2 = the ledger file is missing or the arguments are invalid. Read-only: it never writes to the ledger.

An open TERMINAL (an invariant violation not yet cleared by a human-authorised transition) is reported as a
failed check on purpose. Ordinary PreNode denials do not fail the audit.
Integrity cannot detect truncation; the printed checkpoint exists so an outside party can anchor it.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from vsl_core.ledger import JsonlLedgerStore, VerbaLedger

from app.governance.ledger import DEFAULT_LEDGER_PATH


def installed_vsl_commits() -> dict[str, str]:
    """Best-effort git commit of each installed VSL distribution (from pip's direct_url.json)."""
    found: dict[str, str] = {}
    for dist_name in ("super-semantics-vsl", "vsl-langgraph"):
        try:
            dist = metadata.distribution(dist_name)
            raw = dist.read_text("direct_url.json")
            found[dist_name] = json.loads(raw)["vcs_info"]["commit_id"] if raw else "unknown"
        except (metadata.PackageNotFoundError, OSError, ValueError, KeyError, TypeError):
            found[dist_name] = "unknown"
    return found


PHASE7_CHECK = "recovery_attempt_has_result"


def recovery_attempts_without_result(entries: Iterable) -> list[str]:
    """Phase 7 extension (reported separately from the five VSL checks): every ``recovery_attempt`` record must
    have a result record caused_by it. An attempt with no result means the outcome is unrecorded/uncertain."""
    entries = list(entries)  # callers may pass a generator; this function reads it twice
    attempts = [e.entry_id for e in entries if e.entry_type.value == "MONITOR" and e.payload.get("outcome") == "recovery_attempt"]
    resolved = {e.caused_by for e in entries if e.entry_type.value == "MONITOR"
                and str(e.payload.get("outcome", "")).startswith("recovery_") and e.payload.get("outcome") != "recovery_attempt"}
    return [a for a in attempts if a not in resolved]


def run_audit(path: Path, max_gap: float | None) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {"ledger": str(path), "vsl": installed_vsl_commits()}
    if not path.is_file():
        report["error"] = "ledger file not found"
        return 2, report
    ledger = VerbaLedger(JsonlLedgerStore(path))
    try:
        entries = list(ledger.store.all_entries())
        integrity = ledger.verify_integrity()
    except (ValueError, KeyError, TypeError):
        report.update(entries=None, integrity=False, error="ledger is unreadable or corrupt")
        return 1, report
    report.update(entries=len(entries), integrity=integrity)
    checkpoint = ledger.current_checkpoint()
    report["checkpoint"] = {"sequence": checkpoint.sequence, "entry_hash": checkpoint.entry_hash} if checkpoint else None
    audit = ledger.audit(max_monitor_gap_seconds=max_gap)
    unresolved = recovery_attempts_without_result(entries)
    report.update(checks_passed=list(audit.checks_passed), checks_failed=list(audit.checks_failed),
                  violations=audit.violations,
                  phase7={"check": PHASE7_CHECK, "passed": not unresolved, "violations": unresolved})
    return (0 if integrity and audit.all_passed and not unresolved else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.governance.audit_cli", description=__doc__.split("\n")[0])
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    parser.add_argument("--max-monitor-gap-seconds", type=float, default=None,
                        help="enable check 1 (no_monitoring_gaps); skipped when omitted")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    code, report = run_audit(Path(args.ledger), args.max_monitor_gap_seconds)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return code
    print(f"Ledger: {report['ledger']}")
    print("VSL commits: " + ", ".join(f"{k}={v[:10]}" for k, v in report["vsl"].items()))
    if "error" in report:
        print(f"ERROR: {report.get('error')}")
        print("Result: FAIL" if code == 1 else "Result: NOT AUDITED")
        return code
    print(f"Entries: {report['entries']}   Hash-chain integrity: {'PASS' if report['integrity'] else 'FAIL'}")
    if report.get("checkpoint"):
        print(f"Checkpoint: sequence={report['checkpoint']['sequence']} hash={report['checkpoint']['entry_hash'][:16]}...")
    for name in report["checks_passed"]:
        print(f"  PASS  {name}")
    for name in report["checks_failed"]:
        print(f"  FAIL  {name}: {', '.join(report['violations'][name])}")
    p7 = report["phase7"]
    print(f"  {'PASS' if p7['passed'] else 'FAIL'}  {p7['check']} (Phase 7 extension)"
          + ("" if p7["passed"] else ": " + ", ".join(p7["violations"])))
    print("Note: integrity cannot detect truncation. Max-gap check " + ("ran." if args.max_monitor_gap_seconds is not None else "skipped."))
    print("Result: " + ("PASS" if code == 0 else "FAIL"))
    return code


if __name__ == "__main__":
    sys.exit(main())
