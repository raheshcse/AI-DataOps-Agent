"""Tool 7 - schema-contract validation of the order feed (read-only metadata inspection)."""

from __future__ import annotations

from typing import Any

from app.diagnostics.models import STATUS_ISSUE, STATUS_OK, DiagnosticResult, InvalidInput
from app.simulation.pipelines import feed_table_for
from app.simulation.schema_contract import DRIFTED_FEED_TABLE, FEED_TABLE, ORDER_FEED_CONTRACT, diff_schema
from app.simulation.validators import RULE_ERROR_CODES, RULE_SCHEMA, check_schema

NAME = "validate_schema_contract"
ALLOWED_TABLES = (FEED_TABLE, DRIFTED_FEED_TABLE)


def validate_schema_contract(repo, table: str | None = None) -> DiagnosticResult:
    if table is not None and table not in ALLOWED_TABLES:
        raise InvalidInput(f"table must be one of: {', '.join(ALLOWED_TABLES)}.")
    if table is None:
        active = {sid for sid, row in repo.get_scenarios().items() if row["status"] == "active"}
        checked, how = feed_table_for(active), "default: the table order_validation inspects"
    else:
        checked, how = table, "explicit"
    params = {"table": table}

    observed = repo.observed_schema(checked)  # real database metadata
    diff = diff_schema(ORDER_FEED_CONTRACT, observed)
    verdict = check_schema(observed, checked)
    missing = [{"column": c, "expected_type": ORDER_FEED_CONTRACT[c]} for c in diff["missing"]]
    unexpected = [{"column": c, "observed_type": observed[c]} for c in diff["unexpected"]]
    mismatched = [{"column": c, "expected_type": ORDER_FEED_CONTRACT[c], "observed_type": observed[c]}
                  for c in sorted(set(ORDER_FEED_CONTRACT) & set(observed))
                  if ORDER_FEED_CONTRACT[c] != observed[c]]
    evidence: list[dict[str, Any]] = [
        {"type": "schema_comparison", "checked_table": checked, "table_selection": how,
         "expected_schema": ORDER_FEED_CONTRACT, "observed_schema": observed,
         "missing_columns": missing, "unexpected_columns": unexpected, "type_mismatches": mismatched}]
    counts = {"columns": len(observed), "missing": len(missing), "unexpected": len(unexpected),
              "type_mismatches": len(mismatched)}
    expected = {"columns": len(ORDER_FEED_CONTRACT), "missing": 0, "unexpected": 0, "type_mismatches": 0}
    if verdict.outcome == "pass":
        return DiagnosticResult(NAME, STATUS_OK, "info", f"Table {checked} matches the order feed contract "
                                f"({len(observed)} columns).", evidence, [], counts, expected, None, None, params)
    affected = ([f"column:{m['column']}" for m in missing] + [f"column:{u['column']}" for u in unexpected]
                + [f"column:{m['column']}" for m in mismatched])
    return DiagnosticResult(
        NAME, STATUS_ISSUE, "high",
        f"{verdict.detail} ({len(missing)} missing, {len(unexpected)} unexpected, "
        f"{len(mismatched)} type mismatch(es)).", evidence, affected, counts, expected,
        RULE_ERROR_CODES[RULE_SCHEMA],
        "Compare the expected and observed schemas in the evidence and find which upstream change altered the "
        "feed. This tool does not change any schema.", params)
