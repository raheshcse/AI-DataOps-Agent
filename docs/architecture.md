# Architecture (planned)

> **Status:** this describes the *intended* architecture. As of Phase 1 only the
> FastAPI skeleton (`/health`, `/ready`) and configuration exist. OpenAI calls,
> LangGraph, MCP, VSL governance, recovery and persistence are **not implemented yet**.

## Purpose

An AI DataOps Agent that investigates data pipeline failures using evidence from
real tools, distinguishes confirmed observations from hypotheses, recommends a
corrective action, and performs only governed, human-authorised recovery. If the
evidence is insufficient it reports the root cause as unconfirmed; it never
invents results or claims recovery without independent verification.

## Planned flow

```
Incident
   │
   ▼
FastAPI backend ── validation, authentication, access control, incident lifecycle
   │
   ▼
LangGraph orchestration ── stateful plan → collect evidence → analyse → report
   │        ▲                (pauses for human approval; durable checkpoints)
   │        └── OpenAI API ── incident understanding, planning, interpretation, reporting
   ▼
MCP tool integration ── custom DataOps MCP server exposing validated, read-only diagnostics
   │
   ▼
VSL governance ── VSL-Core evaluates protected actions BEFORE they execute
   │               (via the VSL-LangGraph adapter, subject to compatibility checks)
   ▼
Controlled execution ── restricted, authenticated, duplicate-protected recovery executor
   │
   ▼
Verification and audit evidence ── independent outcome check; investigation,
                                    governance, approval and result records
```

## Component roles

| Component | Role | Not its role |
|---|---|---|
| OpenAI API | Model inference for understanding, planning, interpreting evidence, drafting reports | Security boundary or source of truth for tool results |
| LangGraph | Stateful workflow, tool coordination, human-approval interruptions | Authorisation |
| MCP server | Standard channel to a fixed set of validated diagnostic tools | Authorisation or governance |
| VSL-Core / adapter | Pre-execution governance of protected actions | Replacing app-level validation or OS/database permissions |
| Executor | Performs approved, revalidated actions against configured test resources | Acting on model output directly |

The governance boundary must cover **every** route to a protected action, whether
reached through MCP or directly by the backend. Application validation, access
controls, resource restrictions and database/OS permissions each enforce security
independently.

## Environment and scope

- The project runs against a **simulated operational environment** (synthetic
  SQL Server data, local services, a test pipeline, structured logs, a restricted
  recovery endpoint). There is no company environment.
- It is **general-purpose** and not customised for any particular company.
- It does **not require a DataMart**; SQL Server holds synthetic operational and
  pipeline-related tables only.

## Phase 2 implementation (simulated operational environment)

Implemented in Phase 2 (details in [simulation.md](simulation.md)): a SQL Server schema of ordinary
operational tables, deterministic synthetic data, three simulated pipelines, JSON Lines operational logs,
six controlled failure scenarios, a guarded reset, and read-only `/sim/*` endpoints. This is the *evidence
source* the later agent will investigate. Still **not** implemented: diagnostic tools, MCP, LangGraph,
OpenAI use, VSL governance, recovery. The simulation depends on a storage interface (`SimRepository`), keeping
pipeline logic, database access and API routes separate.

## Phase 1 implementation (what exists today)

- `app/config.py` – Pydantic Settings; secrets held as `SecretStr`, never logged.
- `app/main.py` – FastAPI app with `GET /health` (liveness, no config access) and
  `GET /ready` (config validity and required values, no secret values returned),
  request logging (method, path, status only) and a generic 500 handler.
- `app/core/logging.py` – log redaction for API keys, bearer tokens and similar.
