# Phase 4 - DataOps MCP server

A custom [Model Context Protocol](https://modelcontextprotocol.io) server that exposes the Phase 3 diagnostics
as **eight read-only tools** over stdio, using the official Python SDK (`mcp==2.3.0`). It is the channel a later
agent will use to investigate; in Phase 4 nothing calls OpenAI, changes data, repairs anything or starts a pipeline.

## Design

```
MCP client (Phase 5 agent, MCP Inspector, Claude Desktop ...)
        | JSON-RPC over stdio  (stdout = protocol only, logs -> stderr)
        v
app/mcp_server/server.py     tool schemas, argument guard, result wrapping   <- Phase 4 (adapter only)
        |
app/diagnostics/DiagnosticsService  -> ReadOnlyRepo -> SqlRepository         <- Phase 3 (unchanged)
```

* `server.py` declares each tool and calls the matching `DiagnosticsService` method. All validation, evidence,
  redaction and read-only guarantees stay in Phase 3. Each tool's structured result is exactly
  `DiagnosticResult.to_dict()` (a test compares them for every tool).
* `schemas.py` only *describes* inputs and outputs (types, bounds, enums, output models). A test fails if the
  output models drift from `to_dict()`.
* `provider.py` creates the database connection lazily, on the first tool call, from your normal `.env`
  (the same non-admin connection the CLI uses). The server therefore always starts and lists its tools; if the
  database cannot be reached, tools return a structured `DIAG-DB-ERROR` result with details withheld.
* Tools are `async` and run each diagnostic in a worker thread, so a slow query does not block the protocol.
* `__main__.py` changes to the project root before loading settings, because MCP clients start servers from an
  arbitrary directory. It only *reads* `.env`.

## Tools

| Tool | Inputs | What it does |
|---|---|---|
| `get_pipeline_status` | `pipeline_id?`, `status?` (running/success/warning/failed), `limit` 1-100 (default 10) | Latest run per pipeline plus recent history |
| `inspect_pipeline_logs` | `run_id`, `max_events` 1-1000 (default 200) | Parsed, redacted events from one run's log |
| `detect_duplicate_records` | none | Orders sharing a `source_event_id`; repeated feed events |
| `diagnose_data_quality` | none | Totals vs line items, future timestamps, duplicates, required values, statuses |
| `check_data_freshness` | `pipeline_id` (default `order_ingestion`) | Age of the last successful load vs the threshold |
| `reconcile_record_counts` | `batch_date?` (`YYYY-MM-DD`) | Feed rows vs run metadata vs logs vs orders for a batch |
| `validate_schema_contract` | `table?` (one of the two known feed tables) | Observed vs expected feed-table columns |
| `run_all_diagnostics` | `batch_date?` | Everything above that needs no run id, plus the latest run's log |

All tools carry `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`, and
publish an input schema and an output schema.

### Results and errors

* A **finding** (`ok`, `warning`, `issue_detected`) is a normal result: `isError` is `false`.
* `status: "error"` means the diagnostic could not run (unknown pipeline/run, invalid value the diagnostic
  rejects, unseeded database, database problem). The full structured result (with `error_code`) is still
  returned, and `isError` is `true`. `run_all_diagnostics` reports `isError` when its overall status is `error`.
* Arguments that break the published schema (out-of-range `limit`, wrong type, unknown `status`/`table`, a
  malformed `batch_date`, a missing `run_id`) and argument names a tool does not declare are rejected before
  any diagnostic runs, as an `isError` result with a text message and no structured content.
* Content is returned both as structured content and as the same JSON in a text block.

### What is deliberately not exposed

No SQL, query, statement, path, file, shell or free-form table argument; no insert/update/delete, reset, seed,
scenario activation, pipeline run, recovery or approval tool; no OpenAI call; no VSL. `scenario_id` is not in any
result. A test inspects the tool list, the input schemas and the package source to keep it that way.

## Security notes

* **Read-only is enforced in the application** (`ReadOnlyRepo` allow-list, SELECT-only repository methods, no write
  tool). It is not a database permission: the server uses your normal connection. A `db_datareader`-only login
  would enforce it in SQL Server (still a future hardening step). The server runs as you, locally, over stdio;
  there is no network listener and no authentication layer because there is no remote client.
* **Log text is untrusted.** `inspect_pipeline_logs` returns redacted, length-capped log content, which can still
  contain text that looks like instructions. The server's `instructions` tell a client to treat it as data, but
  the Phase 5 agent must enforce that itself.
* Errors never include driver or connection text. The server never prints to stdout (a test scans the source).

## Run it

```powershell
# from the project root, .venv active
pip install -r requirements.txt            # adds mcp==2.3.0 (and pywin32 on Windows)
python -m scripts.mcp_check --list         # starts the server over stdio, lists the 8 tools; no database used
python -m scripts.mcp_check                # also calls run_all_diagnostics against your configured database
python -m app.mcp_server                   # the server (waits for an MCP client on stdin/stdout)
```

`mcp_check` exit codes: 0 ok/warning, 1 issue detected, 2 the server or a diagnostic could not run.
On the healthy baseline expect every tool `ok` except `reconcile_record_counts`, a low `warning` (the 2026-01-14
batch is pending), as with `scripts.diagnose all --brief`.

### Connecting a client (not verified by me)

Any MCP client that can launch a stdio server works. For example, Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dataops-diagnostics": {
      "command": "C:\\Rahesh\\AI Agents\\AI DataOps Agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": { "PYTHONPATH": "C:\\Rahesh\\AI Agents\\AI DataOps Agent" }
    }
  }
}
```

Use the absolute path of the project's `.venv` Python so the SDK, SQLAlchemy and pyodbc are found. The MCP
Inspector (`npx @modelcontextprotocol/inspector <python> -m app.mcp_server`) also works if you have Node.js.
This configuration has not been tested here; `scripts.mcp_check` is the supported check.

## Tests

```powershell
pytest tests/test_mcp_server.py -v         # unit tests: in-process SDK client + a stdio subprocess; no database
pytest                                     # everything; SQL Server integration tests skip
$env:RUN_SQLSERVER_TESTS = "1"; $env:DB_SERVER = "localhost\SQLEXPRESS03"
pytest tests/integration/test_mcp_sqlserver.py -v -rs      # dataops_sim_test only
Remove-Item Env:RUN_SQLSERVER_TESTS, Env:DB_SERVER
```

The unit tests use the in-memory repository, ignore your `.env` apart from one test that starts the real entry
point and only *lists* tools. The integration tests use the isolated `dataops_sim_test` database, compare every
tool with the direct `DiagnosticsService` result, assert no write statement reaches SQL Server and that the
database fingerprint is unchanged. A skipped test is not a pass.

## Verification status

Executed by Claude in a Linux sandbox with fresh virtual environments built from `requirements.txt`
(no SQL Server, no ODBC driver, no Windows):

| Python | Command | Result |
|--------|---------|--------|
| 3.10.12 | `pytest` | 355 passed, 30 skipped |
| 3.12.14 | `pytest` | 355 passed, 30 skipped |

The 30 skips are all SQL Server integration tests (the 28 from Phases 2-3 plus 2 new MCP ones), skipped because
`RUN_SQLSERVER_TESTS=1` is not set. **A skip is not a pass.** Before Phase 4 the same suite was 300 passed, 28 skipped,
so Phase 4 adds 55 passing unit tests (`tests/test_mcp_server.py`) and 2 skipped integration tests.
Also executed: `python -m scripts.mcp_check --list` (8 tools listed over a real stdio subprocess), and
`python -m scripts.mcp_check` in the sandbox, which correctly reported `DIAG-DB-ERROR` with details withheld
because no database or ODBC driver exists there.

**Not executed by Claude, and therefore not verified:** the 2 new SQL Server integration tests, any run against your
`dataops_sim` or `dataops_sim_test` database, anything on Windows with Python 3.10.11, `pywin32`/SDK behaviour on
Windows, and any real MCP client (Claude Desktop, Inspector). Expected local results: 355 passed and 30 skipped
from plain `pytest` (more skips on Windows only if the symlink test skips: 354 passed, 31 skipped), and 30 passed
from `pytest tests/integration -v -rs` with the gate set. Replace this paragraph with your actual numbers.

## Limitations

* Stdio only. No HTTP/SSE transport, authentication, rate limiting or per-client permissions (not needed for a
  local single-client server; required before any remote use).
* The SDK is a 2.x release with APIs different from the widely documented 1.x (`MCPServer`, not `FastMCP`); the
  version is pinned. Numeric strings such as `"5"` are rejected for integer inputs, and unknown argument names are
  refused by a small middleware in `server.py` because the SDK would otherwise ignore them silently.
* Results can be large (a log tool call may return up to 1000 events). Everything is already bounded by Phase 3.
* The known Phase 2 ingestion limitation (duplicate event ids inside one pending batch are inserted) is unchanged;
  the tools report its effects (see [diagnostics.md](diagnostics.md)).
* Resources and prompts are not exposed; tools only.
* The Phase 5 agent ([agent.md](agent.md)) is the first real client: it discovers these tools at runtime, calls only the
  eight approved read-only ones and validates arguments against the published schemas before calling.
