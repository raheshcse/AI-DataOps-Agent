"""Read-only endpoints for inspecting the simulated environment.

* No endpoint here changes data: no reset, no scenario activation, no pipeline runs.
* Database trouble returns a generic 503; driver messages, connection strings and
  stack traces are never put in a response.
* ``/ready`` is deliberately NOT extended: application readiness (configuration)
  and database health are separate questions. Use ``/sim/db-status`` for the latter.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.connection import (
    DatabaseConfigError,
    DatabaseUnavailable,
    check_connection,
    describe_target,
    make_engine,
)
from app.db.repository import SqlRepository
from app.simulation.clock import SimulationNotSeeded
from app.simulation.ports import SimRepository
from app.simulation.runlog import RunLogStore
from app.simulation.service import SimulationService

logger = logging.getLogger("app.simulation_api")
router = APIRouter(prefix="/sim", tags=["simulation"])

PIPELINE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


# ---------------------------------------------------------------- response models
class DbStatus(BaseModel):
    status: str  # connected | unreachable | misconfigured
    server: str | None = None
    database: str | None = None
    auth: str | None = None
    baseline_seeded: bool | None = None


class PipelineOut(BaseModel):
    pipeline_id: str
    name: str
    description: str
    schedule: str
    expected_records_per_run: int
    is_active: bool


class RunOut(BaseModel):
    run_id: str
    pipeline_id: str
    batch_date: date
    started_at: datetime
    finished_at: datetime | None
    status: str
    duration_ms: int | None
    records_expected: int | None
    records_processed: int | None
    error_code: str | None
    error_message: str | None
    scenario_id: str | None


class ValidationOut(BaseModel):
    rule_name: str
    outcome: str
    expected_value: str | None
    observed_value: str | None
    record_ref: str | None
    detail: str | None


class RunDetailOut(BaseModel):
    run: RunOut
    validations: list[ValidationOut]


class ScenarioOut(BaseModel):
    key: str
    scenario_id: str
    title: str
    status: str
    activated_at: datetime | None


# ------------------------------------------------------------------- dependencies
@lru_cache
def _engine() -> Engine:
    return make_engine(get_settings())


def get_repository() -> SimRepository:
    try:
        return SqlRepository(_engine())
    except (DatabaseConfigError, DatabaseUnavailable, ValidationError) as exc:
        logger.warning("simulation database not usable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Simulation database unavailable") from exc


def get_probe() -> Callable[[], DbStatus]:
    return probe_database


def probe_database() -> DbStatus:
    try:
        settings = get_settings()
    except ValidationError:
        return DbStatus(status="misconfigured")
    target = describe_target(settings)
    shown = {"server": target["server"], "database": target["database"], "auth": target["auth"]}
    try:
        engine = make_engine(settings)
        check_connection(engine)
    except DatabaseConfigError:
        return DbStatus(status="misconfigured", **shown)
    except DatabaseUnavailable:
        return DbStatus(status="unreachable", **shown)
    try:
        seeded: bool | None = SqlRepository(engine).get_state() is not None
    except SQLAlchemyError:
        seeded = None  # connected, but the schema/tables are not there yet
    return DbStatus(status="connected", baseline_seeded=seeded, **shown)


def _guarded(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except SimulationNotSeeded as exc:
        raise HTTPException(status_code=503, detail="Simulation database not seeded") from exc
    except (SQLAlchemyError, DatabaseUnavailable, OSError, ImportError) as exc:
        logger.warning("simulation query failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Simulation database unavailable") from exc


# --------------------------------------------------------------------- endpoints
@router.get("/db-status", response_model=DbStatus)
def db_status(probe: Callable[[], DbStatus] = Depends(get_probe)):
    result = probe()
    code = 200 if result.status == "connected" else 503
    return JSONResponse(status_code=code, content=json.loads(result.model_dump_json()))


@router.get("/pipelines", response_model=list[PipelineOut])
def pipelines(repo: SimRepository = Depends(get_repository)):
    return _guarded(repo.list_pipelines)


@router.get("/runs", response_model=list[RunOut])
def runs(
    pipeline_id: str | None = Query(default=None, pattern=PIPELINE_ID_PATTERN),
    limit: int = Query(default=20, ge=1, le=100),
    repo: SimRepository = Depends(get_repository),
):
    return _guarded(lambda: repo.list_runs(pipeline_id, limit))


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def run_detail(run_id: str = Path(..., pattern=RUN_ID_PATTERN),
               repo: SimRepository = Depends(get_repository)):
    def load():
        run = repo.get_run(run_id)
        if run is None:
            return None
        return {"run": run, "validations": repo.get_validations(run_id)}

    result = _guarded(load)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/scenarios", response_model=list[ScenarioOut])
def scenarios(repo: SimRepository = Depends(get_repository)):
    def load():
        svc = SimulationService(repo, RunLogStore(get_settings().sim_log_dir))
        return svc.scenario_status()
    return _guarded(load)
