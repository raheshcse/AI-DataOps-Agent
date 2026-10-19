"""Phase 8 persistence tests: schema, constraints, transactions, idempotency, restart, and database safety.
Every database here is a temporary SQLite file. SQL Server and dataops_sim are never involved."""

import ast
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from app.persistence import (
    DatabaseConfigError,
    DuplicateExternalRef,
    InvestigationActive,
    OperationsDatabase,
    OperationsStore,
    RecoveryActive,
    StoreError,
    database_path_from_url,
)
from app.persistence.database import LATEST_VERSION

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    db = OperationsDatabase(tmp_path / "ops.sqlite3")
    db.migrate()
    return OperationsStore(db)


def incident(store, **kw):
    args = dict(title="Orders are missing", description="Yesterday's orders are missing from the report", incident_type="other",
                severity="high", external_ref=None, idempotency_key=None, created_by="tester")
    args.update(kw)
    return store.create_incident(**args)[0]


# ------------------------------------------------------------------------------------------ schema
def test_migration_is_idempotent_and_reports_its_version(tmp_path):
    db = OperationsDatabase(tmp_path / "x.sqlite3")
    assert db.schema_version() == 0
    assert db.migrate() == LATEST_VERSION == db.schema_version()
    assert db.migrate() == LATEST_VERSION  # running it again changes nothing


def test_migration_never_discards_existing_data(tmp_path):
    db = OperationsDatabase(tmp_path / "x.sqlite3")
    db.migrate()
    s = OperationsStore(db)
    iid = incident(s)["incident_id"]
    OperationsDatabase(tmp_path / "x.sqlite3").migrate()  # a "second start-up"
    assert OperationsStore(OperationsDatabase(tmp_path / "x.sqlite3")).get_incident(iid) is not None


def test_a_database_from_a_newer_application_is_refused(tmp_path):
    db = OperationsDatabase(tmp_path / "x.sqlite3")
    db.migrate()
    with sqlite3.connect(tmp_path / "x.sqlite3") as conn:
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'x')")
    with pytest.raises(StoreError):
        db.migrate()


def test_a_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    from app.persistence import database as dbmod
    monkeypatch.setattr(dbmod, "MIGRATIONS", [(1, "CREATE TABLE a (x INTEGER); CREATE TABLE a (x INTEGER)")])
    db = OperationsDatabase(tmp_path / "x.sqlite3")
    with pytest.raises(StoreError):
        db.migrate()
    with sqlite3.connect(tmp_path / "x.sqlite3") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


@pytest.mark.parametrize("url", ["mssql+pyodbc://u:p@host/dataops_sim", "postgresql://u@h/db", "sqlite:///:memory:", "sqlite://",
                                 "sqlite://host/db.sqlite3", "http://x"])
def test_only_file_based_sqlite_urls_are_accepted(url):
    with pytest.raises(DatabaseConfigError) as info:
        database_path_from_url(url)
    assert "p@" not in str(info.value) and "u:p" not in str(info.value)  # a URL's credentials never reach an error


def test_url_forms_resolve_to_paths():
    assert database_path_from_url("sqlite:///rel/x.db") == Path("rel/x.db")
    assert database_path_from_url("sqlite:////abs/x.db") == Path("/abs/x.db")
    assert database_path_from_url("sqlite:///C:/data/x.db") == Path("C:/data/x.db")
    assert database_path_from_url(None).name == "operations.sqlite3" and database_path_from_url(" ").parent.name == "data"


def test_the_persistence_layer_cannot_reach_sql_server_or_destroy_data():
    """Static: no SQL Server/SQLAlchemy imports, and no destructive SQL statement anywhere in the package."""
    destructive = re.compile(r"\b(DROP|DELETE|TRUNCATE|ALTER)\b", re.I)
    verb = re.compile(r"^\s*(SELECT|INSERT|UPDATE|CREATE|DROP|DELETE|ALTER|PRAGMA|BEGIN|COMMIT|ROLLBACK|WITH|REPLACE)\b", re.I)
    for path in (ROOT / "app" / "persistence").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                name = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                assert not (name or "").startswith(("sqlalchemy", "pyodbc", "app.db")), (path.name, name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and verb.match(node.value):
                assert not destructive.search(node.value), (path.name, node.value[:60])


# ------------------------------------------------------------------------------------------ incidents
def test_incident_roundtrip_and_utc_timestamps(store):
    row = incident(store, external_ref="TICKET-1")
    assert row["incident_id"].startswith("inc-") and row["status"] == "new"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", row["created_at"])
    assert store.get_incident(row["incident_id"]) == row and store.get_incident("inc-unknown") is None


def test_idempotency_key_returns_the_original_incident(store):
    first, created1 = store.create_incident(title="t t t", description="d" * 12, incident_type="other", severity="low",
                                            external_ref=None, idempotency_key="key-12345678", created_by="a")
    again, created2 = store.create_incident(title="OTHER", description="e" * 12, incident_type="other", severity="high",
                                            external_ref=None, idempotency_key="key-12345678", created_by="b")
    assert created1 and not created2 and again["incident_id"] == first["incident_id"] and again["title"] == "t t t"
    assert store.list_incidents(status=None, severity=None, limit=10, offset=0)[1] == 1


def test_a_reused_external_reference_is_rejected(store):
    first = incident(store, external_ref="REF-1")
    with pytest.raises(DuplicateExternalRef) as info:
        incident(store, external_ref="REF-1")
    assert info.value.incident_id == first["incident_id"]
    assert store.list_incidents(status=None, severity=None, limit=10, offset=0)[1] == 1


def test_listing_paginates_filters_and_orders_newest_first(store):
    ids = [incident(store, severity="high" if i % 2 else "low")["incident_id"] for i in range(5)]
    page, total = store.list_incidents(status=None, severity=None, limit=2, offset=0)
    assert total == 5 and [r["incident_id"] for r in page] == ids[::-1][:2]
    page2, _ = store.list_incidents(status=None, severity=None, limit=2, offset=4)
    assert [r["incident_id"] for r in page2] == [ids[0]]
    assert store.list_incidents(status=None, severity="high", limit=10, offset=0)[1] == 2
    assert store.list_incidents(status="investigated", severity=None, limit=10, offset=0) == ([], 0)


def test_severity_and_status_are_constrained_by_the_database_itself(store, tmp_path):
    with sqlite3.connect(tmp_path / "ops.sqlite3") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO incidents VALUES ('i','e',NULL,'t','d','o','URGENT','new','u','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO incidents VALUES ('i','e',NULL,'t','d','o','low','weird','u','t','t')")


# ------------------------------------------------------------------------------------------ relationships
def test_foreign_keys_are_enforced(store):
    with pytest.raises(StoreError):
        store.create_investigation(incident_id="inc-missing", mode="offline", requested_by="u")
    with pytest.raises(StoreError):
        store.create_recovery(incident_id="inc-missing", investigation_id=None, action_type="rerun_pipeline", parameters={}, requested_by="u")
    with pytest.raises(StoreError):
        store.begin_ticket(incident_id="inc-missing", provider="github_issues", repository="o/r")


# ------------------------------------------------------------------------------------------ investigations
def test_investigation_lifecycle_persists_the_report_and_updates_the_incident_atomically(store):
    iid = incident(store)["incident_id"]
    inv = store.create_investigation(incident_id=iid, mode="offline", requested_by="u")
    assert inv["status"] == "pending" and store.get_incident(iid)["status"] == "investigating"
    assert store.mark_investigation_running(inv["investigation_id"]) and not store.mark_investigation_running(inv["investigation_id"])
    assert store.complete_investigation(inv["investigation_id"], report={"a": 1, "nested": {"b": [1, 2]}}, report_status="issue_identified",
                                        summary="s", evidence_refs=["E1", "E2"])
    got = store.get_investigation(inv["investigation_id"])
    assert got["status"] == "completed" and got["report"] == {"a": 1, "nested": {"b": [1, 2]}} and got["evidence_refs"] == ["E1", "E2"]
    assert store.get_incident(iid)["status"] == "investigated"
    assert not store.complete_investigation(inv["investigation_id"], report={}, report_status="x", summary="", evidence_refs=[])  # once


def test_only_one_investigation_may_be_active_per_incident(store):
    iid = incident(store)["incident_id"]
    first = store.create_investigation(incident_id=iid, mode="offline", requested_by="u")
    with pytest.raises(InvestigationActive) as info:
        store.create_investigation(incident_id=iid, mode="offline", requested_by="u")
    assert info.value.investigation_id == first["investigation_id"]
    store.fail_investigation(first["investigation_id"], error_kind="boom", error_message="x")
    assert store.get_incident(iid)["status"] == "investigation_failed"
    again = store.create_investigation(incident_id=iid, mode="offline", requested_by="u")  # allowed once the first has ended
    assert again["investigation_id"] != first["investigation_id"]


def test_concurrent_investigation_requests_create_exactly_one(store):
    iid = incident(store)["incident_id"]
    outcomes, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            store.create_investigation(incident_id=iid, mode="offline", requested_by="u")
            outcomes.append("created")
        except InvestigationActive:
            outcomes.append("duplicate")

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("created") == 1 and outcomes.count("duplicate") == 7


def test_a_failure_inside_a_transaction_rolls_everything_back(store, tmp_path):
    iid = incident(store)["incident_id"]
    inv = store.create_investigation(incident_id=iid, mode="offline", requested_by="u")
    with sqlite3.connect(tmp_path / "ops.sqlite3") as conn:  # make the SECOND statement of complete_investigation fail
        conn.execute("CREATE TRIGGER boom BEFORE UPDATE OF status ON incidents WHEN NEW.status='investigated' "
                     "BEGIN SELECT RAISE(ABORT,'boom'); END")
    with pytest.raises(StoreError):
        store.complete_investigation(inv["investigation_id"], report={"x": 1}, report_status="r", summary="s", evidence_refs=[])
    after = store.get_investigation(inv["investigation_id"])
    assert after["status"] == "pending" and after["report"] is None  # the first UPDATE was rolled back too
    assert store.get_incident(iid)["status"] == "investigating"


def test_startup_sweep_fails_unfinished_investigations_and_keeps_finished_ones(store):
    a, b = incident(store)["incident_id"], incident(store)["incident_id"]
    done = store.create_investigation(incident_id=a, mode="offline", requested_by="u")
    store.complete_investigation(done["investigation_id"], report={}, report_status="r", summary="s", evidence_refs=[])
    running = store.create_investigation(incident_id=b, mode="offline", requested_by="u")
    store.mark_investigation_running(running["investigation_id"])
    assert store.sweep_interrupted_investigations() == 1
    swept = store.get_investigation(running["investigation_id"])
    assert swept["status"] == "failed" and swept["error_kind"] == "interrupted"
    assert store.get_investigation(done["investigation_id"])["status"] == "completed"
    assert store.get_incident(b)["status"] == "investigation_failed" and store.get_incident(a)["status"] == "investigated"
    assert store.sweep_interrupted_investigations() == 0


# ------------------------------------------------------------------------------------------ recoveries
def make_recovery(store, iid):
    return store.create_recovery(incident_id=iid, investigation_id=None, action_type="rerun_pipeline",
                                 parameters={"pipeline_id": "order_ingestion"}, requested_by="u")


def test_one_active_recovery_per_incident_and_a_new_one_after_it_ends(store):
    iid = incident(store)["incident_id"]
    first = make_recovery(store, iid)
    with pytest.raises(RecoveryActive) as info:
        make_recovery(store, iid)
    assert info.value.recovery_id == first["recovery_id"]
    assert store.record_recovery_outcome(first["recovery_id"], status="rejected")
    assert make_recovery(store, iid)["recovery_id"] != first["recovery_id"]


def test_the_resume_claim_has_exactly_one_winner(store):
    rec = make_recovery(store, incident(store)["incident_id"])
    winners, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        winners.append(store.claim_recovery_resume(rec["recovery_id"]))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert winners.count(True) == 1
    assert store.get_recovery(rec["recovery_id"])["status"] == "resuming"
    assert store.release_recovery_claim(rec["recovery_id"]) and store.claim_recovery_resume(rec["recovery_id"])


def test_a_final_recovery_outcome_is_never_rewritten(store):
    rec = make_recovery(store, incident(store)["incident_id"])
    assert store.record_recovery_outcome(rec["recovery_id"], status="executed", execution_status="executed", verified_outcome="verified",
                                         reasons=["r"], ledger_entry_ids=["e1", "e2"], decision_id="dec-1", approval_request_id="apr-1", started=True)
    assert not store.record_recovery_outcome(rec["recovery_id"], status="rejected")  # cannot flip a finished recovery
    got = store.get_recovery(rec["recovery_id"])
    assert (got["status"], got["verified_outcome"], got["reasons"], got["ledger_entry_ids"]) == ("executed", "verified", ["r"], ["e1", "e2"])
    assert got["completed_at"] and got["started_at"]
    assert store.get_recovery_by_request("apr-1")["recovery_id"] == rec["recovery_id"]


def test_an_invalid_verified_outcome_is_rejected_by_the_database(store):
    rec = make_recovery(store, incident(store)["incident_id"])
    with pytest.raises(StoreError):
        store.record_recovery_outcome(rec["recovery_id"], status="executed", verified_outcome="definitely")


# ------------------------------------------------------------------------------------------ tickets
def test_ticket_claim_semantics(store):
    iid = incident(store)["incident_id"]
    assoc, claimed = store.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    assert claimed and assoc["status"] == "creating" and assoc["external_id"] is None
    again, claimed2 = store.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    assert not claimed2 and again["status"] == "creating"          # someone else is mid-flight
    store.finish_ticket(iid, status="creation_unconfirmed", sync_result="timeout")
    retry, claimed3 = store.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    assert claimed3 and retry["status"] == "creating"              # a deliberate retry may proceed
    store.finish_ticket(iid, status="open", external_id="9001", external_number=7, url="https://github.com/o/r/issues/7", sync_result="ok")
    done, claimed4 = store.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    assert not claimed4 and done["external_number"] == 7 and done["status"] == "open"


def test_the_same_external_issue_cannot_be_attached_to_two_incidents(store):
    a, b = incident(store)["incident_id"], incident(store)["incident_id"]
    for iid in (a, b):
        store.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    store.finish_ticket(a, status="open", external_id="1", external_number=5, url="u", sync_result="ok")
    with pytest.raises(StoreError):
        store.finish_ticket(b, status="open", external_id="1", external_number=5, url="u", sync_result="ok")


def test_ticket_events_are_recorded_newest_first(store):
    iid = incident(store)["incident_id"]
    store.add_ticket_event(iid, "create", "failed", "timeout")
    store.add_ticket_event(iid, "create", "created")
    events = store.list_ticket_events(iid)
    assert [e["outcome"] for e in events] == ["created", "failed"] and events[1]["error_kind"] == "timeout"


# ------------------------------------------------------------------------------------------ restart & failure
def test_everything_survives_a_restart(tmp_path):
    path = tmp_path / "ops.sqlite3"
    first = OperationsStore(OperationsDatabase(path))
    first.database.migrate()
    iid = incident(first, external_ref="R-9")["incident_id"]
    inv = first.create_investigation(incident_id=iid, mode="offline", requested_by="u")
    first.complete_investigation(inv["investigation_id"], report={"k": "v"}, report_status="issue_identified", summary="s", evidence_refs=["E1"])
    rec = make_recovery(first, iid)
    first.record_recovery_outcome(rec["recovery_id"], status="awaiting_approval", approval_request_id="apr-" + "a" * 32)
    first.begin_ticket(incident_id=iid, provider="github_issues", repository="o/r")
    first.finish_ticket(iid, status="open", external_id="1", external_number=3, url="https://x/3", sync_result="ok")

    second = OperationsStore(OperationsDatabase(path))        # a "new process"
    second.database.migrate()
    assert second.get_incident(iid)["status"] == "investigated"
    assert second.latest_completed_investigation(iid)["report"] == {"k": "v"}
    assert second.get_recovery(rec["recovery_id"])["status"] == "awaiting_approval"   # NOT turned into anything else
    assert second.get_ticket(iid)["external_number"] == 3


def test_an_unusable_database_surfaces_as_a_safe_store_error(tmp_path):
    (tmp_path / "isadir").mkdir()
    broken = OperationsStore(OperationsDatabase(tmp_path / "isadir"))
    with pytest.raises(StoreError) as info:
        broken.get_incident("x")
    assert str(tmp_path) not in str(info.value) and info.value.reason in ("cannot_open_database", "storage_error")
