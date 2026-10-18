"""Relational schema for the simulated operational environment (SQLAlchemy Core).

Conventions
-----------
* snake_case names; ``*_id`` primary keys; explicit foreign keys and CHECK constraints.
* Timestamps are stored as DATETIME2(3) in **UTC** and returned as timezone-aware UTC
  datetimes (see ``UTCDateTime``). This keeps SQL Server comparisons simple and
  avoids driver-specific DATETIMEOFFSET handling.
* Rows created for a failure scenario carry ``scenario_id`` so they are always
  distinguishable from baseline data and can be removed precisely.
* There is no analytics warehouse or Datamart: these are ordinary operational tables.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UnicodeText,
    UniqueConstraint,
)
from sqlalchemy.dialects import mssql
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Stores tz-aware datetimes as naive UTC DATETIME2(3); returns tz-aware UTC."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mssql":
            return dialect.type_descriptor(mssql.DATETIME2(precision=3))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware (UTC).")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value


metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
    }
)

customers = Table(
    "customers",
    metadata,
    Column("customer_id", Integer, primary_key=True, autoincrement=False),
    Column("customer_code", String(20), nullable=False),
    Column("full_name", String(100), nullable=False),
    Column("email", String(150), nullable=False),
    Column("region", String(30), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    UniqueConstraint("customer_code"),
)

products = Table(
    "products",
    metadata,
    Column("product_id", Integer, primary_key=True, autoincrement=False),
    Column("sku", String(20), nullable=False),
    Column("name", String(100), nullable=False),
    Column("category", String(50), nullable=False),
    Column("unit_price", Numeric(10, 2), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    UniqueConstraint("sku"),
    CheckConstraint("unit_price >= 0", name="unit_price_nonneg"),
)

ORDER_STATUSES = ("placed", "paid", "shipped", "cancelled")

orders = Table(
    "orders",
    metadata,
    Column("order_id", Integer, primary_key=True, autoincrement=False),
    Column("customer_id", Integer, ForeignKey("customers.customer_id"), nullable=False),
    # Identifier of the source event; NOT unique on purpose: duplicate source events
    # are a failure mode this project must be able to detect (scenario 3).
    Column("source_event_id", String(40), nullable=False),
    Column("batch_date", Date, nullable=False),
    Column("order_ts", UTCDateTime, nullable=False),
    Column("status", String(20), nullable=False),
    Column("total_amount", Numeric(12, 2), nullable=False),
    Column("currency", String(3), nullable=False, server_default="NZD"),
    Column("scenario_id", String(40), nullable=True),
    CheckConstraint(
        "status IN ('placed','paid','shipped','cancelled')", name="status_valid"
    ),
    CheckConstraint("total_amount >= 0", name="total_nonneg"),
    Index("ix_orders_source_event_id", "source_event_id"),
    Index("ix_orders_batch_date", "batch_date"),
    Index("ix_orders_customer_id", "customer_id"),
)

order_items = Table(
    "order_items",
    metadata,
    Column("order_item_id", Integer, primary_key=True, autoincrement=False),
    Column("order_id", Integer, ForeignKey("orders.order_id"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.product_id"), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Numeric(10, 2), nullable=False),
    Column("line_total", Numeric(12, 2), nullable=False),
    UniqueConstraint("order_id", "line_no", name="uq_order_items_order_line"),
    CheckConstraint("quantity > 0", name="quantity_positive"),
    Index("ix_order_items_order_id", "order_id"),
    Index("ix_order_items_product_id", "product_id"),
)

# Raw incoming order feed (what the ingestion pipeline reads). No foreign keys: a
# staging feed is untrusted input.
def _feed_columns() -> list[Column]:
    return [
        Column("feed_id", Integer, primary_key=True, autoincrement=False),
        Column("batch_date", Date, nullable=False),
        Column("source_event_id", String(40), nullable=False),
        Column("customer_id", Integer, nullable=False),
        Column("order_ts", UTCDateTime, nullable=False),
        Column("order_total", Numeric(12, 2), nullable=False),
        Column("currency", String(3), nullable=False),
        Column("items_json", UnicodeText, nullable=False),
        Column("scenario_id", String(40), nullable=True),
    ]


stg_order_feed = Table(
    "stg_order_feed",
    metadata,
    *_feed_columns(),
    Index("ix_stg_order_feed_batch_date", "batch_date"),
)

# Isolated fixture for the schema-mismatch scenario: a copy of the feed table with
# deliberate drift (column ``currency`` missing, ``legacy_flag`` extra, ``order_total``
# of the wrong type). The real feed table is never altered.
fx_order_feed_drifted = Table(
    "fx_order_feed_drifted",
    metadata,
    Column("feed_id", Integer, primary_key=True, autoincrement=False),
    Column("batch_date", Date, nullable=False),
    Column("source_event_id", String(40), nullable=False),
    Column("customer_id", Integer, nullable=False),
    Column("order_ts", UTCDateTime, nullable=False),
    Column("order_total", String(20), nullable=False),
    Column("items_json", UnicodeText, nullable=False),
    Column("scenario_id", String(40), nullable=True),
    Column("legacy_flag", Boolean, nullable=True),
)

pipeline_definitions = Table(
    "pipeline_definitions",
    metadata,
    Column("pipeline_id", String(50), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("description", String(500), nullable=False),
    Column("schedule", String(100), nullable=False),
    Column("expected_records_per_run", Integer, nullable=False),
    Column("config_json", UnicodeText, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="1"),
)

RUN_STATUSES = ("running", "success", "warning", "failed")

pipeline_runs = Table(
    "pipeline_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("pipeline_id", String(50), ForeignKey("pipeline_definitions.pipeline_id"), nullable=False),
    Column("batch_date", Date, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime, nullable=True),
    Column("status", String(20), nullable=False),
    Column("duration_ms", Integer, nullable=True),
    Column("records_expected", Integer, nullable=True),
    Column("records_processed", Integer, nullable=True),
    Column("error_code", String(20), nullable=True),
    Column("error_message", String(500), nullable=True),
    Column("scenario_id", String(200), nullable=True),
    Column("trigger_type", String(20), nullable=False, server_default="manual"),
    CheckConstraint("status IN ('running','success','warning','failed')", name="status_valid"),
    Index("ix_pipeline_runs_pipeline_started", "pipeline_id", "started_at"),
    Index("ix_pipeline_runs_status", "status"),
)

validation_results = Table(
    "validation_results",
    metadata,
    Column("result_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), ForeignKey("pipeline_runs.run_id"), nullable=False),
    Column("rule_name", String(80), nullable=False),
    Column("outcome", String(10), nullable=False),
    Column("expected_value", String(200), nullable=True),
    Column("observed_value", String(200), nullable=True),
    Column("record_ref", String(400), nullable=True),
    Column("detail", String(1000), nullable=True),
    Column("created_at", UTCDateTime, nullable=False),
    CheckConstraint("outcome IN ('pass','fail','warn')", name="outcome_valid"),
    Index("ix_validation_results_run_id", "run_id"),
)

incident_records = Table(
    "incident_records",
    metadata,
    Column("incident_id", String(20), primary_key=True),
    Column("category", String(40), nullable=False),
    Column("severity", String(10), nullable=False),
    Column("title", String(200), nullable=False),
    Column("description", String(1000), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("pipeline_id", String(50), ForeignKey("pipeline_definitions.pipeline_id"), nullable=True),
    Column("run_id", String(64), ForeignKey("pipeline_runs.run_id"), nullable=True),
    Column("scenario_id", String(40), nullable=True),
    Column("evidence_ref", String(400), nullable=True),
    CheckConstraint("severity IN ('low','medium','high','critical')", name="severity_valid"),
    CheckConstraint("status IN ('open','investigating','resolved')", name="status_valid"),
    Index("ix_incident_records_run_id", "run_id"),
)

scenario_activations = Table(
    "scenario_activations",
    metadata,
    Column("scenario_id", String(40), primary_key=True),
    Column("status", String(10), nullable=False),
    Column("activated_at", UTCDateTime, nullable=False),
    Column("restored_at", UTCDateTime, nullable=True),
    Column("details_json", UnicodeText, nullable=False),
    CheckConstraint("status IN ('active','restored')", name="status_valid"),
)

sim_state = Table(
    "sim_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("baseline_version", String(20), nullable=False),
    Column("seed", Integer, nullable=False),
    Column("sim_now", UTCDateTime, nullable=False),
    Column("current_batch_date", Date, nullable=False),
    Column("seeded_at", UTCDateTime, nullable=False),
    CheckConstraint("id = 1", name="single_row"),
)

# Tables in dependency order (parents first). Deleting uses the reverse order.
TABLE_ORDER = [
    customers,
    products,
    orders,
    order_items,
    stg_order_feed,
    fx_order_feed_drifted,
    pipeline_definitions,
    pipeline_runs,
    validation_results,
    incident_records,
    scenario_activations,
    sim_state,
]
TABLE_NAMES = [t.name for t in TABLE_ORDER]
