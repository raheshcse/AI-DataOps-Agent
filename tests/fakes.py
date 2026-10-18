"""In-memory SimRepository used by the unit tests (NOT a SQLite/SQL Server stand-in).

It mirrors the semantics the simulation relies on so pipeline/scenario *logic* can be
tested without a database. SQL behaviour is only proven by the integration tests.
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal

from app.db import schema as s
from app.simulation.schema_contract import ORDER_FEED_CONTRACT

DRIFTED_OBSERVED = {
    "feed_id": "int", "batch_date": "date", "source_event_id": "string", "customer_id": "int",
    "order_ts": "datetime", "order_total": "string", "items_json": "string",
    "scenario_id": "string", "legacy_flag": "bool",
}


def enforce_column_lengths(table, row: dict) -> None:
    """Raise like SQL Server (error 2628) when a string exceeds its column length.

    The fake used to accept any string, which let an over-long ``scenario_id`` pass
    every unit test and fail only against the real database.
    """
    for name, value in row.items():
        column = table.c.get(name)
        length = getattr(column.type, "length", None) if column is not None else None
        if isinstance(value, str) and length is not None and len(value) > length:
            raise ValueError(
                f"String or binary data would be truncated in table '{table.name}', "
                f"column '{name}'. Truncated value: '{value[:length]}'")


class InMemoryRepo:
    def __init__(self):
        self._clear()

    def _clear(self):
        self.state = None
        self.customers, self.products, self.orders, self.items = [], [], [], []
        self.feed, self.defs, self.runs, self.vals, self.incidents = [], [], [], [], []
        self.scenarios: dict[str, dict] = {}

    # state / baseline
    def get_state(self):
        return dict(self.state) if self.state else None

    def set_sim_now(self, when):
        self.state["sim_now"] = when

    def is_empty(self):
        return not self.customers

    def seed(self, data):
        self.customers = copy.deepcopy(data.customers)
        self.products = copy.deepcopy(data.products)
        for o in copy.deepcopy(data.orders):
            for it in o.pop("items"):
                self.items.append(dict(it, order_id=o["order_id"]))
            self.orders.append(o)
        self.feed = copy.deepcopy(data.feed_rows)
        self.defs = copy.deepcopy(data.pipeline_definitions)
        self.runs = copy.deepcopy(data.runs)
        self.vals = [dict(v, result_id=i) for i, v in enumerate(copy.deepcopy(data.validations), 1)]
        self.incidents = copy.deepcopy(data.incidents)
        self.state = dict(data.state)

    def table_counts(self):
        return {"customers": len(self.customers), "products": len(self.products),
                "orders": len(self.orders), "order_items": len(self.items),
                "stg_order_feed": len(self.feed), "fx_order_feed_drifted": 0,
                "pipeline_definitions": len(self.defs), "pipeline_runs": len(self.runs),
                "validation_results": len(self.vals), "incident_records": len(self.incidents),
                "scenario_activations": len(self.scenarios), "sim_state": 1 if self.state else 0}

    def integrity_problems(self):
        problems = []
        cust = {c["customer_id"] for c in self.customers}
        order_ids = {o["order_id"] for o in self.orders}
        run_ids = {r["run_id"] for r in self.runs}
        if any(o["customer_id"] not in cust for o in self.orders):
            problems.append("orders without customer")
        if any(i["order_id"] not in order_ids for i in self.items):
            problems.append("items without order")
        if any(v["run_id"] not in run_ids for v in self.vals):
            problems.append("validations without run")
        return problems

    def tagged_row_counts(self):
        return {"orders": sum(1 for o in self.orders if o.get("scenario_id")),
                "stg_order_feed": sum(1 for f in self.feed if f.get("scenario_id")),
                "incident_records": sum(1 for i in self.incidents if i.get("scenario_id"))}

    def wipe(self):
        counts = {k: v for k, v in self.table_counts().items()}
        self._clear()
        return counts

    # definitions, runs, incidents
    def list_pipelines(self):
        return sorted(copy.deepcopy(self.defs), key=lambda d: d["pipeline_id"])

    def get_pipeline(self, pid):
        return next((copy.deepcopy(d) for d in self.defs if d["pipeline_id"] == pid), None)

    def list_runs(self, pipeline_id=None, limit=20):
        rows = [r for r in self.runs if pipeline_id in (None, r["pipeline_id"])]
        rows.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
        return copy.deepcopy(rows[:limit])

    def get_run(self, run_id):
        return next((copy.deepcopy(r) for r in self.runs if r["run_id"] == run_id), None)

    def run_exists(self, run_id):
        return any(r["run_id"] == run_id for r in self.runs)

    def latest_successful_run(self, pid):
        ok = [r for r in self.runs if r["pipeline_id"] == pid and r["status"] == "success"]
        return copy.deepcopy(max(ok, key=lambda r: r["finished_at"])) if ok else None

    def save_run(self, run, validations, incident):
        # Validate everything first so a rejected row leaves nothing behind, matching the
        # single SQL transaction used by SqlRepository.save_run.
        enforce_column_lengths(s.pipeline_runs, run)
        for v in validations:
            enforce_column_lengths(s.validation_results, v)
        if incident:
            enforce_column_lengths(s.incident_records, incident)
        self.runs.append(copy.deepcopy(run))
        for v in validations:
            self.vals.append(dict(copy.deepcopy(v), result_id=len(self.vals) + 1))
        if incident:
            self.incidents.append(copy.deepcopy(incident))

    def next_incident_id(self):
        last = max((int(i["incident_id"].split("-")[1]) for i in self.incidents), default=0)
        return f"INC-{last + 1:04d}"

    def list_incidents(self, limit=20):
        return copy.deepcopy(sorted(self.incidents, key=lambda i: i["created_at"], reverse=True)[:limit])

    def get_validations(self, run_id):
        return copy.deepcopy([v for v in self.vals if v["run_id"] == run_id])

    # pipeline data
    def get_feed_rows(self, batch_date: date):
        return copy.deepcopy(sorted((f for f in self.feed if f["batch_date"] == batch_date),
                                    key=lambda f: f["feed_id"]))

    def known_event_ids(self, event_ids):
        wanted = set(event_ids)
        return {o["source_event_id"] for o in self.orders if o["source_event_id"] in wanted}

    def insert_orders(self, orders):
        next_order = max((o["order_id"] for o in self.orders), default=0) + 1
        next_item = max((i["order_item_id"] for i in self.items), default=0) + 1
        for offset, o in enumerate(copy.deepcopy(orders)):
            oid = next_order + offset
            items = o.pop("items")
            self.orders.append(dict(o, order_id=oid))
            for it in items:
                self.items.append(dict(it, order_id=oid, order_item_id=next_item))
                next_item += 1
        return len(orders)

    def order_summary(self, batch_date):
        rows = [o for o in self.orders if o["batch_date"] == batch_date]
        return {"order_count": len(rows), "revenue": sum((o["total_amount"] for o in rows), Decimal("0")),
                "distinct_customers": len({o["customer_id"] for o in rows})}

    def duplicate_events(self):
        groups: dict[str, list[int]] = {}
        for o in sorted(self.orders, key=lambda o: o["order_id"]):
            groups.setdefault(o["source_event_id"], []).append(o["order_id"])
        return [{"source_event_id": e, "copies": len(ids), "order_ids": ids}
                for e, ids in sorted(groups.items()) if len(ids) > 1]

    def order_total_mismatches(self):
        out = []
        for o in sorted(self.orders, key=lambda o: o["order_id"]):
            total = sum((i["line_total"] for i in self.items if i["order_id"] == o["order_id"]), Decimal("0"))
            if total != o["total_amount"]:
                out.append({"order_id": o["order_id"], "source_event_id": o["source_event_id"],
                            "total_amount": o["total_amount"], "items_total": total})
        return out

    def future_dated_orders(self, now: datetime):
        return [{"order_id": o["order_id"], "source_event_id": o["source_event_id"], "order_ts": o["order_ts"]}
                for o in sorted(self.orders, key=lambda o: o["order_id"]) if o["order_ts"] > now]

    def sample_orders(self, limit):
        rows = sorted((o for o in self.orders if not o.get("scenario_id")), key=lambda o: o["order_id"])[:limit]
        return [dict(copy.deepcopy(o), items=[copy.deepcopy(i) for i in self.items
                                              if i["order_id"] == o["order_id"]]) for o in rows]

    def observed_schema(self, table_name):
        if table_name == "stg_order_feed":
            return dict(ORDER_FEED_CONTRACT)
        if table_name == "fx_order_feed_drifted":
            return dict(DRIFTED_OBSERVED)
        raise ValueError("not readable")

    # scenarios
    def get_scenarios(self):
        return copy.deepcopy(self.scenarios)

    def save_scenario(self, row):
        self.scenarios[row["scenario_id"]] = copy.deepcopy(row)

    def delete_scenario_rows(self, scenario_id):
        ids = {o["order_id"] for o in self.orders if o.get("scenario_id") == scenario_id}
        self.items = [i for i in self.items if i["order_id"] not in ids]
        self.orders = [o for o in self.orders if o["order_id"] not in ids]
        return len(ids)
