"""SQL Server implementation of ``SimRepository`` (SQLAlchemy Core).

Every query is built with SQLAlchemy expressions and bound parameters; no SQL text
is assembled from input. The only schema-introspection entry point
(``observed_schema``) is restricted to a fixed list of table names.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Engine,
    and_,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import mssql
from sqlalchemy.types import TypeDecorator
from sqlalchemy import types as sqltypes

from app.db import schema as s

READABLE_SCHEMA_TABLES = {"stg_order_feed", "fx_order_feed_drifted"}
_CHUNK = 500  # stay far below SQL Server's 2100-parameter limit


def type_family(sa_type: Any) -> str:
    """Normalise a SQLAlchemy / reflected SQL Server type to a coarse family."""
    if isinstance(sa_type, TypeDecorator):
        sa_type = sa_type.impl
    if isinstance(sa_type, (sqltypes.Boolean, mssql.BIT)):
        return "bool"
    if isinstance(sa_type, sqltypes.Integer):
        return "int"
    if isinstance(sa_type, sqltypes.Numeric):  # includes Float / DECIMAL
        return "decimal"
    if isinstance(sa_type, sqltypes.DateTime):
        return "datetime"
    if isinstance(sa_type, sqltypes.Date):
        return "date"
    if isinstance(sa_type, sqltypes.String):  # includes Text / Unicode / NVARCHAR
        return "string"
    return type(sa_type).__name__.lower()


def _rows(result) -> list[dict[str, Any]]:
    return [dict(r) for r in result.mappings().all()]


class SqlRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------- state / baseline
    def get_state(self) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(s.sim_state)).mappings().first()
            return dict(row) if row else None

    def set_sim_now(self, when: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(update(s.sim_state).where(s.sim_state.c.id == 1).values(sim_now=when))

    def is_empty(self) -> bool:
        with self.engine.connect() as c:
            return c.execute(select(func.count()).select_from(s.customers)).scalar_one() == 0

    def seed(self, data: Any) -> None:
        """Insert the whole baseline in a single transaction (all or nothing)."""
        orders = [{k: v for k, v in o.items() if k != "items"} for o in data.orders]
        items = [dict(it, order_id=o["order_id"]) for o in data.orders for it in o["items"]]
        with self.engine.begin() as c:
            c.execute(insert(s.customers), data.customers)
            c.execute(insert(s.products), data.products)
            c.execute(insert(s.pipeline_definitions), data.pipeline_definitions)
            c.execute(insert(s.orders), orders)
            c.execute(insert(s.order_items), items)
            c.execute(insert(s.stg_order_feed), data.feed_rows)
            c.execute(insert(s.pipeline_runs), data.runs)
            c.execute(insert(s.validation_results), data.validations)
            if data.incidents:
                c.execute(insert(s.incident_records), data.incidents)
            c.execute(insert(s.sim_state), data.state)

    def table_counts(self) -> dict[str, int]:
        with self.engine.connect() as c:
            return {t.name: c.execute(select(func.count()).select_from(t)).scalar_one()
                    for t in s.TABLE_ORDER}

    def integrity_problems(self) -> list[str]:
        links = [
            (s.orders, "customer_id", s.customers, "customer_id"),
            (s.order_items, "order_id", s.orders, "order_id"),
            (s.order_items, "product_id", s.products, "product_id"),
            (s.pipeline_runs, "pipeline_id", s.pipeline_definitions, "pipeline_id"),
            (s.validation_results, "run_id", s.pipeline_runs, "run_id"),
            (s.incident_records, "run_id", s.pipeline_runs, "run_id"),
        ]
        problems = []
        with self.engine.connect() as c:
            for child, ccol, parent, pcol in links:
                q = (select(func.count()).select_from(
                        child.outerjoin(parent, child.c[ccol] == parent.c[pcol]))
                     .where(and_(parent.c[pcol].is_(None), child.c[ccol].is_not(None))))
                n = c.execute(q).scalar_one()
                if n:
                    problems.append(f"{n} {child.name}.{ccol} value(s) have no parent in {parent.name}")
        return problems

    def tagged_row_counts(self) -> dict[str, int]:
        with self.engine.connect() as c:
            return {t.name: c.execute(select(func.count()).select_from(t)
                                      .where(t.c.scenario_id.is_not(None))).scalar_one()
                    for t in (s.orders, s.stg_order_feed, s.incident_records)}

    def wipe(self) -> dict[str, int]:
        deleted = {}
        with self.engine.begin() as c:
            for t in reversed(s.TABLE_ORDER):
                deleted[t.name] = c.execute(delete(t)).rowcount or 0
        return deleted

    # ----------------------------------------------- definitions, runs, incidents
    def list_pipelines(self) -> list[dict]:
        with self.engine.connect() as c:
            return _rows(c.execute(select(s.pipeline_definitions).order_by(s.pipeline_definitions.c.pipeline_id)))

    def get_pipeline(self, pipeline_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(s.pipeline_definitions)
                            .where(s.pipeline_definitions.c.pipeline_id == pipeline_id)).mappings().first()
            return dict(row) if row else None

    def list_runs(self, pipeline_id: str | None = None, limit: int = 20, status: str | None = None) -> list[dict]:
        q = select(s.pipeline_runs).order_by(s.pipeline_runs.c.started_at.desc(), s.pipeline_runs.c.run_id.desc())
        if pipeline_id:
            q = q.where(s.pipeline_runs.c.pipeline_id == pipeline_id)
        if status:
            q = q.where(s.pipeline_runs.c.status == status)
        with self.engine.connect() as c:
            return _rows(c.execute(q.limit(limit)))

    def get_run(self, run_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(s.pipeline_runs).where(s.pipeline_runs.c.run_id == run_id)).mappings().first()
            return dict(row) if row else None

    def run_exists(self, run_id: str) -> bool:
        return self.get_run(run_id) is not None

    def latest_successful_run(self, pipeline_id: str) -> dict | None:
        q = (select(s.pipeline_runs)
             .where(and_(s.pipeline_runs.c.pipeline_id == pipeline_id, s.pipeline_runs.c.status == "success"))
             .order_by(s.pipeline_runs.c.finished_at.desc()).limit(1))
        with self.engine.connect() as c:
            row = c.execute(q).mappings().first()
            return dict(row) if row else None

    def save_run(self, run: dict, validations: list[dict], incident: dict | None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(s.pipeline_runs), run)
            if validations:
                c.execute(insert(s.validation_results), validations)
            if incident:
                c.execute(insert(s.incident_records), incident)

    def next_incident_id(self) -> str:
        with self.engine.connect() as c:
            last = c.execute(select(func.max(s.incident_records.c.incident_id))).scalar_one()
        return f"INC-{(int(last.split('-')[1]) if last else 0) + 1:04d}"

    def list_incidents(self, limit: int = 20) -> list[dict]:
        with self.engine.connect() as c:
            return _rows(c.execute(select(s.incident_records)
                                   .order_by(s.incident_records.c.created_at.desc()).limit(limit)))

    def get_validations(self, run_id: str) -> list[dict]:
        with self.engine.connect() as c:
            return _rows(c.execute(select(s.validation_results)
                                   .where(s.validation_results.c.run_id == run_id)
                                   .order_by(s.validation_results.c.result_id)))

    # --------------------------------------------- data the pipelines read / write
    def get_feed_rows(self, batch_date: date) -> list[dict]:
        with self.engine.connect() as c:
            return _rows(c.execute(select(s.stg_order_feed)
                                   .where(s.stg_order_feed.c.batch_date == batch_date)
                                   .order_by(s.stg_order_feed.c.feed_id)))

    def known_event_ids(self, event_ids: list[str]) -> set[str]:
        found: set[str] = set()
        with self.engine.connect() as c:
            for i in range(0, len(event_ids), _CHUNK):
                chunk = event_ids[i:i + _CHUNK]
                q = select(s.orders.c.source_event_id).distinct().where(s.orders.c.source_event_id.in_(chunk))
                found.update(c.execute(q).scalars().all())
        return found

    def insert_orders(self, orders: list[dict]) -> int:
        if not orders:
            return 0
        with self.engine.begin() as c:
            next_order = (c.execute(select(func.coalesce(func.max(s.orders.c.order_id), 0))).scalar_one()) + 1
            next_item = (c.execute(select(func.coalesce(func.max(s.order_items.c.order_item_id), 0))).scalar_one()) + 1
            order_rows, item_rows = [], []
            for offset, o in enumerate(orders):
                oid = next_order + offset
                order_rows.append({k: v for k, v in o.items() if k != "items"} | {"order_id": oid})
                for it in o["items"]:
                    item_rows.append(dict(it, order_id=oid, order_item_id=next_item))
                    next_item += 1
            c.execute(insert(s.orders), order_rows)
            c.execute(insert(s.order_items), item_rows)
        return len(orders)

    def order_summary(self, batch_date: date) -> dict:
        q = select(func.count(), func.coalesce(func.sum(s.orders.c.total_amount), 0),
                   func.count(func.distinct(s.orders.c.customer_id))).where(s.orders.c.batch_date == batch_date)
        with self.engine.connect() as c:
            count, revenue, customers = c.execute(q).one()
        return {"order_count": count, "revenue": revenue, "distinct_customers": customers}

    def duplicate_events(self) -> list[dict]:
        o = s.orders
        with self.engine.connect() as c:
            dups = c.execute(select(o.c.source_event_id, func.count().label("copies"))
                             .group_by(o.c.source_event_id).having(func.count() > 1)
                             .order_by(o.c.source_event_id)).all()
            if not dups:
                return []
            ids_by_event: dict[str, list[int]] = {}
            names = [d[0] for d in dups]
            for i in range(0, len(names), _CHUNK):
                for oid, ev in c.execute(select(o.c.order_id, o.c.source_event_id)
                                         .where(o.c.source_event_id.in_(names[i:i + _CHUNK]))
                                         .order_by(o.c.order_id)):
                    ids_by_event.setdefault(ev, []).append(oid)
        return [{"source_event_id": ev, "copies": n, "order_ids": ids_by_event.get(ev, [])} for ev, n in dups]

    def order_total_mismatches(self) -> list[dict]:
        o, i = s.orders, s.order_items
        items_total = func.coalesce(func.sum(i.c.line_total), 0)
        q = (select(o.c.order_id, o.c.source_event_id, o.c.total_amount, items_total.label("items_total"))
             .select_from(o.outerjoin(i, i.c.order_id == o.c.order_id))
             .group_by(o.c.order_id, o.c.source_event_id, o.c.total_amount)
             .having(o.c.total_amount != items_total).order_by(o.c.order_id))
        with self.engine.connect() as c:
            return _rows(c.execute(q))

    def future_dated_orders(self, now: datetime) -> list[dict]:
        o = s.orders
        with self.engine.connect() as c:
            return _rows(c.execute(select(o.c.order_id, o.c.source_event_id, o.c.order_ts)
                                   .where(o.c.order_ts > now).order_by(o.c.order_id)))

    def sample_orders(self, limit: int) -> list[dict]:
        o, i = s.orders, s.order_items
        with self.engine.connect() as c:
            orders = _rows(c.execute(select(o).where(o.c.scenario_id.is_(None))
                                     .order_by(o.c.order_id).limit(limit)))
            if not orders:
                return []
            items = _rows(c.execute(select(i).where(i.c.order_id.in_([x["order_id"] for x in orders]))
                                    .order_by(i.c.order_id, i.c.line_no)))
        for order in orders:
            order["items"] = [it for it in items if it["order_id"] == order["order_id"]]
        return orders

    # ------------------------------------------- read-only detail queries (diagnostics)
    def duplicate_order_details(self) -> list[dict]:
        """Orders whose ``source_event_id`` occurs more than once, grouped per event id."""
        o = s.orders
        repeated = select(o.c.source_event_id).group_by(o.c.source_event_id).having(func.count() > 1)
        with self.engine.connect() as c:
            rows = _rows(c.execute(
                select(o.c.order_id, o.c.source_event_id, o.c.customer_id, o.c.batch_date, o.c.order_ts,
                       o.c.total_amount, o.c.currency)
                .where(o.c.source_event_id.in_(repeated)).order_by(o.c.source_event_id, o.c.order_id)))
        groups: dict[str, list[dict]] = {}
        for r in rows:  # SQL Server compares ids case-insensitively, so group the same way
            groups.setdefault(r["source_event_id"].strip().casefold(), []).append(r)
        return [{"source_event_id": g[0]["source_event_id"], "copies": len(g), "orders": g}
                for g in groups.values() if len(g) > 1]

    def duplicate_feed_events(self) -> list[dict]:
        f = s.stg_order_feed
        repeated = select(f.c.source_event_id).group_by(f.c.source_event_id).having(func.count() > 1)
        with self.engine.connect() as c:
            rows = _rows(c.execute(select(f.c.feed_id, f.c.source_event_id, f.c.batch_date)
                                   .where(f.c.source_event_id.in_(repeated))
                                   .order_by(f.c.source_event_id, f.c.feed_id)))
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["source_event_id"].strip().casefold(), []).append(r)
        return [{"source_event_id": g[0]["source_event_id"], "copies": len(g),
                 "feed_ids": [r["feed_id"] for r in g], "batch_dates": sorted({r["batch_date"] for r in g})}
                for g in groups.values() if len(g) > 1]

    def order_quality_scan(self) -> dict[str, list[dict]]:
        """Identifier-level findings for blank required values, negative amounts and bad statuses."""
        o, i = s.orders, s.order_items
        blank = or_(func.ltrim(func.rtrim(o.c.source_event_id)) == "", func.ltrim(func.rtrim(o.c.currency)) == "")
        with self.engine.connect() as c:
            blank_rows = _rows(c.execute(select(o.c.order_id, o.c.source_event_id, o.c.currency)
                                         .where(blank).order_by(o.c.order_id)))
            neg_orders = _rows(c.execute(select(o.c.order_id, o.c.source_event_id, o.c.total_amount)
                                         .where(o.c.total_amount < 0).order_by(o.c.order_id)))
            neg_items = _rows(c.execute(select(i.c.order_item_id, i.c.order_id, i.c.quantity, i.c.unit_price,
                                               i.c.line_total)
                                        .where(or_(i.c.quantity <= 0, i.c.unit_price < 0, i.c.line_total < 0))
                                        .order_by(i.c.order_item_id)))
            bad_status = _rows(c.execute(select(o.c.order_id, o.c.source_event_id, o.c.status)
                                         .where(o.c.status.not_in(s.ORDER_STATUSES)).order_by(o.c.order_id)))
        negative = ([dict(r, kind="order") for r in neg_orders] + [dict(r, kind="order_item") for r in neg_items])
        return {"blank_required": blank_rows, "negative_amounts": negative, "invalid_status": bad_status}

    def observed_schema(self, table_name: str) -> dict[str, str]:
        if table_name not in READABLE_SCHEMA_TABLES:
            raise ValueError("Schema inspection is limited to the order feed tables.")
        columns = inspect(self.engine).get_columns(table_name)
        return {col["name"]: type_family(col["type"]) for col in columns}

    # ------------------------------------------------------------------- scenarios
    def get_scenarios(self) -> dict[str, dict]:
        with self.engine.connect() as c:
            return {r["scenario_id"]: r for r in _rows(c.execute(select(s.scenario_activations)))}

    def save_scenario(self, row: dict) -> None:
        t = s.scenario_activations
        with self.engine.begin() as c:
            result = c.execute(update(t).where(t.c.scenario_id == row["scenario_id"]).values(**row))
            if result.rowcount == 0:
                c.execute(insert(t).values(**row))

    def delete_scenario_rows(self, scenario_id: str) -> int:
        with self.engine.begin() as c:
            ids = list(c.execute(select(s.orders.c.order_id).where(s.orders.c.scenario_id == scenario_id)).scalars())
            if ids:
                c.execute(delete(s.order_items).where(s.order_items.c.order_id.in_(ids)))
                c.execute(delete(s.orders).where(s.orders.c.order_id.in_(ids)))
        return len(ids)
