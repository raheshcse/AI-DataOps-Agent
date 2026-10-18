"""Phase 6 ledger and audit tests: causal links, SUFFICIENT vs INSUFFICIENT, tamper, semantic audit, CLI."""

import json
import subprocess
import sys

import pytest
from vsl_core.ledger import LedgerEntryType, VerbaLedger, VerificationResult

from app.governance import audit_cli, open_ledger
from tests.governance_support import good, make

ID = "dataops-agent-governance"


def fresh(tmp_path):
    return open_ledger(tmp_path / "l.jsonl", fsync=False)


def test_clean_traffic_passes_integrity_and_all_five_checks(tmp_path):
    service, rec, _ = make(tmp_path)
    for _ in range(3):
        service.govern_sync(good())
    assert rec.ledger.verify_integrity()
    report = rec.ledger.audit(max_monitor_gap_seconds=60)
    assert report.all_passed and len(report.checks_passed) == 5


def test_a_drift_flagged_monitor_without_a_pre_node_fails_check_2(tmp_path):
    ledger = fresh(tmp_path)
    ledger.write_monitor(identity_key=ID, drift_detected=True)
    assert ledger.audit().checks_failed == ("drift_flagged_monitor_has_pre_node",)


def test_a_pre_node_without_verification_fails_check_3(tmp_path):
    ledger = fresh(tmp_path)
    m = ledger.write_monitor(identity_key=ID, drift_detected=True)
    ledger.write(LedgerEntryType.PRE_NODE, identity_key=ID, caused_by=m.entry_id)
    assert ledger.audit().checks_failed == ("pre_node_has_verification",)


def test_a_pre_node_whose_causal_link_points_elsewhere_does_not_resolve_the_monitor(tmp_path):
    ledger = fresh(tmp_path)
    m = ledger.write_monitor(identity_key=ID, drift_detected=True)
    ledger.write(LedgerEntryType.PRE_NODE, identity_key=ID, caused_by="some-other-entry")
    assert "drift_flagged_monitor_has_pre_node" in ledger.audit().checks_failed


def test_insufficient_verification_needs_a_specification_update_caused_by_it(tmp_path):
    ledger = fresh(tmp_path)
    m = ledger.write_monitor(identity_key=ID, drift_detected=False)
    v = ledger.write_verification(identity_key=ID, result=VerificationResult.INSUFFICIENT, caused_by=m.entry_id)
    assert ledger.audit().checks_failed == ("insufficient_verification_has_specification_update",)
    ledger.write(LedgerEntryType.SPECIFICATION_UPDATE, identity_key=ID, caused_by="unrelated")
    assert ledger.audit().checks_failed == ("insufficient_verification_has_specification_update",)
    ledger.write(LedgerEntryType.SPECIFICATION_UPDATE, identity_key=ID, caused_by=v.entry_id)
    assert ledger.audit().all_passed


def test_a_terminal_without_a_human_authorised_transition_fails_check_5(tmp_path):
    ledger = fresh(tmp_path)
    t = ledger.write(LedgerEntryType.TERMINAL, identity_key=ID)
    assert ledger.audit().checks_failed == ("terminal_has_human_authorised_transition",)
    ledger.write(LedgerEntryType.HUMAN_AUTHORISED_TRANSITION, identity_key=ID, caused_by=t.entry_id)
    assert ledger.audit().all_passed


def test_monitoring_gaps_are_only_checked_when_a_threshold_is_given(tmp_path):
    ledger = fresh(tmp_path)
    ledger.write_monitor(identity_key=ID, drift_detected=False)
    ledger.write_monitor(identity_key=ID, drift_detected=False)
    assert ledger.audit().all_passed
    assert ledger.audit(max_monitor_gap_seconds=-1).checks_failed == ("no_monitoring_gaps",)


def test_editing_any_byte_of_the_file_breaks_integrity(tmp_path):
    service, rec, _ = make(tmp_path)
    service.govern_sync(good())
    service.govern_sync(good(approval=None))
    path = rec.ledger.store.path
    assert rec.ledger.verify_integrity()
    path.write_text(path.read_text().replace("preconditions_failed", "preconditions_fixed"), encoding="utf-8")
    assert not rec.ledger.verify_integrity()


def test_deleting_a_middle_entry_breaks_integrity_but_truncation_is_not_detected(tmp_path):
    service, rec, _ = make(tmp_path)
    for _ in range(3):
        service.govern_sync(good())
    path = rec.ledger.store.path
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert not rec.ledger.verify_integrity()
    path.write_text("\n".join(lines[:-2]) + "\n")  # drop the last two entries
    assert rec.ledger.verify_integrity()  # documented limit: truncation is invisible to the chain check


def run_cli(*args):
    return audit_cli.main(list(args))


def test_cli_exit_codes(tmp_path, capsys):
    service, rec, _ = make(tmp_path)
    service.govern_sync(good())
    service.govern_sync(good(approval=None))
    path = str(rec.ledger.store.path)
    assert run_cli("--ledger", path) == 0
    out = capsys.readouterr().out
    assert "Hash-chain integrity: PASS" in out and "PASS  terminal_has_human_authorised_transition" in out
    assert "vsl_core" not in out and "OPENAI" not in out
    # open incident -> semantic failure -> non-zero
    service.govern_sync(good(action="drop_all_tables"))
    assert run_cli("--ledger", path) == 1
    assert "FAIL  terminal_has_human_authorised_transition" in capsys.readouterr().out
    # missing file -> 2, and the CLI must not create it
    missing = tmp_path / "nope.jsonl"
    assert run_cli("--ledger", str(missing)) == 2 and not missing.exists()
    # tamper -> 1
    p = tmp_path / "t.jsonl"
    s2, r2, _ = make(tmp_path, name="t.jsonl")
    s2.govern_sync(good())
    p.write_text(p.read_text().replace("alice", "eve"))
    assert run_cli("--ledger", str(p)) == 1
    # corrupt file -> 1
    p.write_text("garbage\n")
    assert run_cli("--ledger", str(p)) == 1
    assert run_cli("--bogus") == 2


def test_cli_json_output_and_module_entry(tmp_path):
    service, rec, _ = make(tmp_path)
    service.govern_sync(good(approval=None))
    out = subprocess.run([sys.executable, "-m", "app.governance.audit_cli", "--ledger", str(rec.ledger.store.path), "--json"],
                         capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["integrity"] is True and data["entries"] == 3 and data["checks_failed"] == []


def test_the_ledger_does_not_store_secrets_or_raw_incident_text(tmp_path):
    service, rec, _ = make(tmp_path)
    service.govern_sync(good(action="x" * 500))
    text = rec.ledger.store.path.read_text()
    assert "x" * 101 not in text  # the untrusted action string is bounded before it is recorded
