"""Expected schema of the order feed and a pure schema-diff function."""

from __future__ import annotations

# column name -> normalised type family (see app.db.repository.type_family)
ORDER_FEED_CONTRACT: dict[str, str] = {
    "feed_id": "int",
    "batch_date": "date",
    "source_event_id": "string",
    "customer_id": "int",
    "order_ts": "datetime",
    "order_total": "decimal",
    "currency": "string",
    "items_json": "string",
    "scenario_id": "string",
}

FEED_TABLE = "stg_order_feed"
DRIFTED_FEED_TABLE = "fx_order_feed_drifted"


def diff_schema(expected: dict[str, str], observed: dict[str, str]) -> dict[str, list[str]]:
    """Compare two ``{column: type_family}`` mappings.

    Returns lists of ``missing`` (expected but absent), ``unexpected`` (present but not
    expected) and ``type_mismatch`` (``"col: expected X, observed Y"``).
    """
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    mismatched = [
        f"{col}: expected {expected[col]}, observed {observed[col]}"
        for col in sorted(set(expected) & set(observed))
        if expected[col] != observed[col]
    ]
    return {"missing": missing, "unexpected": unexpected, "type_mismatch": mismatched}
