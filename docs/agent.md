# Phase 5 - the AI DataOps investigation agent

Takes an incident description ("yesterday's orders are missing from the sales report"), gathers evidence from
the Phase 4 MCP server, and returns a structured investigation report. OpenAI does the reasoning, LangGraph
orchestrates, MCP is the only route to data. It **investigates only**: it has no write, recovery or governance
capability (those are Phases 6-7).

> **Verification status (read this first).** The whole workflow is tested with a scripted fake model, a
> deterministic offline engine and the real MCP client/server. **No request has ever been sent to OpenAI**: live
> model behaviour (plan quality, report quality, structured-output acceptance by your chosen model, latency,
> token use and **cost**) is **unverified**. The live path is exercised only against fakes of the OpenAI client.
> Nothing here is production-ready.

## Architecture

```
incident --> receive_incident --> plan --> execute_tools --> evaluate --+--> plan ... (bounded loop)
                                    \__________________________________/
                                                         \--> generate_report --> InvestigationReport
```

| Piece | File | Role |
|---|---|---|
| Config, key state | `app/agent/config.py` | `AgentConfig` (frozen, key never printed), `classify_key`, `MissingApiKey`, budgets |
| Model interface + OpenAI adapter | `app/agent/llm.py` | `ChatModel` protocol; `OpenAIChatModel` (strict JSON-schema output, no tools, errors mapped to fixed key-free messages) |
| Offline engine | `app/agent/offline.py` | `OfflineHeuristicModel`: deterministic keyword rules - **not a language model** |
| MCP client | `app/agent/mcp_client.py` | `Toolbox`: starts `python -m app.mcp_server` over stdio, discovers tools at runtime, validates arguments against the discovered schemas, timeouts, response checks, clean shutdown |
| Workflow | `app/agent/graph.py` | LangGraph state machine; budgets; failure handling |
| Evidence + report | `app/agent/report.py`, `models.py` | digests, status, grounding checks, confidence caps, text rendering |
| Safety helpers | `app/agent/safety.py` | incident validation, redaction, injection markers, bounded digests |
| Prompts | `app/agent/prompts.py` | system prompts and payloads |
| Entry points | `scripts/investigate.py`, `scripts/openai_smoke.py` | CLI; optional billable smoke test |

### The design rule: the model interprets, code verifies

* **Facts come from tools.** "Observed facts" in the report are the diagnostics' own summaries. The model never
  writes them.
* **Every model claim is checked.** Hypotheses, the root cause and the impact must cite evidence ids that exist;
  a root cause must cite an actual finding (a warning or issue), not a passing check. Statements naming run ids,
  error codes, `DIAG-` codes or dates that are not in the evidence are dropped, and the report says how many.
* **Status is computed, not asked for.** `issue_identified` / `warnings_only` / `no_issue_detected` /
  `inconclusive` / `failed` follow from the evidence. `no_issue_detected` requires all six core diagnostics to
  have produced a usable result; otherwise the answer is `inconclusive` and the missing checks are listed.
* **Confidence is capped by rules**: one supporting diagnostic caps it at medium; an undetermined cause, low
  severity, or a non-issue status caps it at low; failed tool calls cap it below high. The model's requested
  level is only an upper bound.
* **A passing or successful call never means "resolved".** Reports say so explicitly.

### Safety model

| Concern | Control |
|---|---|
| Arbitrary actions | The agent calls only the 8 names in `APPROVED_TOOLS` that the server *also* advertises read-only. Everything else the server offers is ignored and listed in the report. There is no SQL, shell, file or write tool, and `app/agent` cannot import the database, diagnostics or simulation code (a test enforces this). |
| Bad arguments | Validated against the **discovered** JSON schema before the call; unknown argument names are refused. The server validates again. |
| Runaway loops / cost | Budgets: rounds (3), tool calls (12), calls per round (4), invalid calls (3), evidence size, prompt size. Duplicate calls are skipped. After an infrastructure failure the rest of the round is skipped. |
| Prompt injection | Incident text and all tool output are placed under `untrusted_evidence`/`incident`, and the prompts say to treat them as data. **That is not the defence.** The structural defences are: the model can only choose approved read-only tools; the offline engine reads structured fields only; claims are verified against evidence; evidence containing instruction-like text is flagged in `security_notes` and is **not accepted as support for a root cause or impact** (it stays visible as an observation). |
| Secrets | The key is a `SecretStr`, never in `repr`, logs, progress, reports or error text. Provider error text is never copied (it can echo key fragments). Everything stored or printed passes through the redactor, including the incident text and tool output. |
| Failure | MCP/DB/model failures become a `failed` report (exit 2), not a traceback. Server subprocess is stopped on success, error and timeout. |

Known limit of the injection defence: it reduces risk, it does not eliminate it. A hostile log line that is
phrased in a way the markers do not match is treated as ordinary evidence, and a live model could be influenced
by it when interpreting; the verification layer still limits what such influence can put in the report.

## Running it

### Offline (no key, no network, no cost)

```powershell
python -m scripts.investigate "Yesterday's orders are missing from the sales report" --offline
python -m scripts.investigate "The nightly pipeline failed" --offline --json
python -m scripts.investigate --check-config        # key state and budgets; no network, no MCP
```

This starts the MCP server as a subprocess; it reads your `.env` and runs the **read-only** diagnostics against
your configured database (normally `dataops_sim`). It changes nothing. Offline mode picks tools by keyword and
ranks findings by status/severity; it exists to exercise the whole pipeline and for demos without a key. Its
reports are labelled `MODE: OFFLINE heuristic`.

Flags: `--json`, `--no-evidence`, `--quiet` (progress goes to stderr), `--model`, `--max-rounds` (1-6),
`--max-tool-calls` (1-20). Optional env budgets: `AGENT_MAX_ROUNDS`, `AGENT_MAX_TOOL_CALLS`,
`AGENT_MAX_CALLS_PER_ROUND`, `AGENT_TOOL_TIMEOUT_SECONDS`, `AGENT_LLM_TIMEOUT_SECONDS`, `AGENT_LLM_MAX_RETRIES`,
`AGENT_MAX_INCIDENT_CHARS`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `no_issue_detected` or `warnings_only` |
| 1 | `issue_identified` or `inconclusive` |
| 2 | `failed` (MCP, database or model error), or configuration/key problem |
| 3 | invalid input (empty/too long incident, bad option), or a refused action |

### Live mode (uses OpenAI; billable)

```powershell
python -m scripts.investigate "Yesterday's orders are missing from the sales report"
```

Without a usable `OPENAI_API_KEY` this stops immediately with exit 2, before starting MCP or touching the
network. A placeholder (the `.env.example` value), a blank or a malformed key counts as missing. (The Phase 1
`/ready` endpoint only checks that a value is present, so it can say "configured" for a placeholder; the agent is
stricter.) The default model `gpt-4o-mini` is a placeholder in `.env.example`: set `OPENAI_MODEL` to a model your
account has that supports **structured outputs with a strict JSON schema** - not verified.

### Adding the key without exposing it (PowerShell, project root)

```powershell
$secure = Read-Host "OpenAI API key" -AsSecureString          # typed input is not echoed
$plain  = [System.Net.NetworkCredential]::new('', $secure).Password
$lines  = if (Test-Path .env) { [string[]](Get-Content .env) } else { @() }
$found  = $false
$lines  = $lines | ForEach-Object { if ($_ -match '^OPENAI_API_KEY=') { $found = $true; "OPENAI_API_KEY=$plain" } else { $_ } }
if (-not $found) { $lines += "OPENAI_API_KEY=$plain" }
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) ".env"), $lines, (New-Object System.Text.UTF8Encoding $false))  # UTF-8 without BOM
Remove-Variable plain, secure, lines
python -m scripts.investigate --check-config                    # must say: OPENAI_API_KEY : configured  (never the value)
git status --short                                              # .env must not appear (it is git-ignored)
```

Do not paste the key into the terminal as a command argument or into chat; it would land in shell history.

### Optional live smoke test (one tiny billable request)

```powershell
python -m scripts.openai_smoke                      # prints what it would do; sends nothing
python -m scripts.openai_smoke --yes-call-openai    # ONE small structured-output request
```

It proves only that the key is accepted and the model answers a strict-schema request. It does not validate
planning or report quality.

## Reading a report

| Section | How to read it |
|---|---|
| **Status** | Computed from evidence. `issue_identified`: a diagnostic confirmed a problem. `warnings_only`: only low-severity warnings and every core check ran. `no_issue_detected`: every core check ran clean - the symptom could not be reproduced, **not** proof it never happened. `inconclusive`: not enough usable evidence (a check failed, was not run, or a medium/high warning exists without a confirmed issue). `failed`: no usable evidence (server, database or model failure). |
| **Observed facts** | Verbatim tool findings with evidence ids. |
| **Hypotheses** | Model interpretation, each citing evidence. Not facts. |
| **Likely root cause** | `supported` only if cited evidence is an actual finding; otherwise `Undetermined`. |
| **Confidence** | low/medium/high with reasons. Treat `high` as "two or more independent diagnostics agree", not as certainty. |
| **Limitations** | Failed/skipped/budget-limited calls, unchecked diagnostics, truncation. Read these before acting. |
| **Security notes** | Instruction-like text found in tool output. |

Evidence ids: `E3` is the third tool call; `E4.2` is the second diagnostic inside a `run_all_diagnostics` call.

## Tests

Counts for the current tree are in [Verification status](#verification-status). Layers:

| File | What | Uses |
|---|---|---|
| `tests/test_agent_unit.py` | key handling, config, OpenAI adapter (fake client), incident validation, injection markers, argument validation, malformed model output, report grounding/confidence/status, offline engine, static "no DB / no shell" guarantees | fakes only |
| `tests/test_agent_graph.py` | workflow transitions, tool selection, budgets, tool and model failures, malformed output, injection, secrecy | scripted model + scripted toolbox |
| `tests/test_agent_mcp.py` | **real MCP client and server**: discovery, invocation, structured output, invalid args, timeouts, malformed responses, connection failure, subprocess cleanup, stdio, no writes; the six scenarios, healthy baseline, ambiguous and insufficient evidence, database-down | real MCP server (in-process and stdio subprocess) over the in-memory repository; offline engine or scripted model |
| `tests/test_agent_cli.py` | CLI exit codes, JSON/text output, missing key, no secrets, smoke script | in-memory MCP; fake live model |
| `tests/integration/test_agent_sqlserver.py` | the same path over real T-SQL; no write statements; database unchanged | **SQL Server, `dataops_sim_test` only**; gated by `RUN_SQLSERVER_TESTS=1` |

```powershell
pytest                                                                  # everything that needs no database
$env:RUN_SQLSERVER_TESTS='1'; $env:DB_SERVER='.\SQLEXPRESS03'
pytest tests/integration/test_agent_sqlserver.py -v -rs                 # SQL Server, test database only
```

A skipped test is not a pass.

## Verification status

| Python | Command | Result |
|--------|---------|--------|
| 3.10.12 | `pytest` | 576 passed, 40 skipped |
| 3.12.14 | `pytest` | 576 passed, 40 skipped |

Environment: Linux sandbox, clean virtualenvs built from `requirements.txt`. The 40 skips are all SQL Server
integration tests (30 from Phases 2-4 plus 10 new), skipped because `RUN_SQLSERVER_TESTS=1` is not set.
Phases 1-4 contribute the same 355 passing tests as before; Phase 5 adds 221 (119 unit, 37 workflow, 44 MCP/scenario,
21 CLI).

**Not executed, therefore not verified:** the 10 new SQL Server tests (their logic was dry-run once against an
in-memory repository to catch test bugs - that is *not* a SQL Server run); anything on Windows or Python 3.10.11;
the stdio subprocess on Windows (`pywin32`); every live OpenAI interaction; billing. Expected local results with
the gate set: 40 passed from `pytest -m integration`; plain `pytest` on Windows may show 575 passed / 41 skipped
if the symlink test skips there (as in Phase 4). Replace this paragraph with your actual numbers.

## Limitations

* No live model validation. Planning and report quality, instruction-following under injection, structured-output
  support for your chosen model, token use and cost are unknown.
* Offline mode is keyword rules, not reasoning; it can pick the wrong tools for unusual wording (it widens to a
  full diagnostic sweep when targeted checks find nothing, which is the safety net).
* Evidence reaches the model as bounded digests; very large log output is truncated (flagged in the report).
* Root-cause statements are leads from diagnostics that count findings; they are not proof of causation.
* The injection markers are heuristics (see above). Evidence tripping the strongest markers cannot support a
  root cause even if the text was genuine; the report says so.
* One investigation at a time; reports are printed, not stored (persistence is a later phase).
* The agent starts its own MCP server process per run and therefore reads whatever database `.env` points to.
* `jsonschema` and `anyio` are used directly but arrive as dependencies of `mcp`; they are not separately pinned.
* The known Phase 2 ingestion limitation (duplicate event ids inside one pending batch are inserted) is
  unchanged and surfaces in findings as described in [diagnostics.md](diagnostics.md).

## Phase 5 versus Phase 6 (governance)

Phase 5 is *advisory and read-only*: it recommends investigation steps and human actions. It has no approval
flow, no policy engine, no audit trail beyond the printed report, no VSL integration and no way to change data.
Phase 6 adds VSL governance around the agent's decisions; Phase 7 adds human approval and controlled recovery.
Nothing in Phase 5 should be read as a governance control; the controls above are engineering safeguards for a
read-only investigator.

## Manual Windows checklist

Run from the project root with the venv active. Nothing below resets or seeds a database.

1. `git status --short` - review the Phase 5 changes (nothing is committed yet).
2. `pip install -r requirements.txt` (adds `langgraph==1.2.16`).
3. `pytest` - expect about 576 passed / 40 skipped (575 / 41 if the symlink test skips on Windows).
4. `python -m scripts.investigate --check-config` - without a key it says `placeholder` or `missing` and exits 2; that is correct.
5. `python -m scripts.investigate "Orders look wrong" --offline` - needs your `dataops_sim` database and the MCP server; healthy baseline gives exit 0 and a `warnings_only` report with an undetermined cause.
6. SQL Server integration (isolated database only):
   `$env:RUN_SQLSERVER_TESTS='1'; $env:DB_SERVER='.\SQLEXPRESS03'; pytest -m integration -v -rs` - expect 40 passed, 0 skipped. Then `Remove-Item Env:RUN_SQLSERVER_TESTS`.
7. Add the key (section above) and re-run step 4: it must say `configured`. Confirm `git status` does not list `.env`.
8. **Billable:** `python -m scripts.openai_smoke --yes-call-openai`.
9. **Billable, optional:** `python -m scripts.investigate "Orders look wrong"` and compare with the offline report.
10. `python -m scripts.db_verify` - confirm the `dataops_sim` baseline is still healthy.
