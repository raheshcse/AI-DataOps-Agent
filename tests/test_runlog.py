import json

import pytest

from app.simulation.runlog import RunLogStore


def event(run_id="RUN-ING-1", level="INFO"):
    return {"timestamp": "2026-01-15T01:00:00.000Z", "level": level, "component": "c",
            "pipeline_id": "order_ingestion", "run_id": run_id, "event": "e", "message": "m",
            "error_code": None, "scenario_id": None, "data": {}}


def test_write_read_roundtrip_is_json_lines(tmp_path):
    store = RunLogStore(tmp_path)
    path = store.write("order_ingestion", "RUN-ING-1", [event(), event(level="ERROR")])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["level"] == "ERROR"
    assert [e["level"] for e in store.read("RUN-ING-1")] == ["INFO", "ERROR"]
    assert store.relative_ref("order_ingestion", "RUN-ING-1") == "pipeline_runs/order_ingestion/RUN-ING-1.jsonl"


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "..", "", "x" * 200, "a b"])
def test_path_traversal_is_rejected(tmp_path, bad):
    store = RunLogStore(tmp_path)
    with pytest.raises(ValueError):
        store.write("order_ingestion", bad, [event()])
    with pytest.raises(ValueError):
        store.read(bad)


def test_read_unknown_run_returns_empty_and_clear_only_removes_run_logs(tmp_path):
    store = RunLogStore(tmp_path)
    assert store.read("RUN-NOPE") == []
    store.write("order_ingestion", "RUN-ING-1", [event()])
    keep = tmp_path / "keep.txt"
    keep.write_text("x")
    assert store.clear() == 1 and keep.exists() and store.read("RUN-ING-1") == []
