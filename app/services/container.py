"""Wire the Phase 8 services from the environment.

Every optional capability fails SAFE and independently: a missing signing key, a misconfigured verifier, an
unconfigured GitHub repository or a non-test simulation database each disable only their own feature, with a stated
reason that readiness reports (names only, never values). Nothing destructive happens here: the operational schema
is created/upgraded additively and unfinished investigations from a previous process are marked failed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.agent import AgentConfig, ChatModel, OfflineHeuristicModel, OpenAIChatModel, load_agent_config
from app.config import Settings, get_settings
from app.governance import GovernedActionService, build_policy, open_ledger
from app.governance.ledger import GovernanceRecorder
from app.integrations import GitHubConfig, GitHubIssuesAdapter
from app.persistence import OperationsDatabase, OperationsStore, database_path_from_url
from app.recovery import (
    RECOVERY_ACTIONS,
    ApprovalConfigError,
    ApprovalService,
    ApprovalSigner,
    DenyAllAuthenticator,
    RecoveryExecutor,
    RecoveryRunner,
    RecoveryWorkflow,
    SqliteApprovalStore,
    build_authenticator,
)
from app.recovery.auth import Authenticator
from app.services.investigations import InvestigationService
from app.services.recoveries import RecoveryService
from app.services.throttle import AuthThrottle
from app.services.tickets import TicketService

logger = logging.getLogger("app.services")


@dataclass
class Services:
    store: OperationsStore
    authenticator: Authenticator
    investigations: InvestigationService
    tickets: TicketService
    app_env: str = "development"
    auth_status: str = "disabled"            # disabled | dev_static | misconfigured
    approvals: ApprovalService | None = None
    recorder: GovernanceRecorder | None = None
    recoveries: RecoveryService | None = None
    recovery_unavailable: str | None = "not_configured"
    ticket_status: str = "not_configured"    # not_configured | configured | misconfigured
    throttle: AuthThrottle = field(default_factory=AuthThrottle)
    notes: dict[str, str] = field(default_factory=dict)


def _llm_factory(mode: str, config: AgentConfig) -> Callable[[], ChatModel]:
    if mode == "live":
        return lambda: OpenAIChatModel(config)  # raises MissingApiKey when no usable key: recorded as a failed run
    return lambda: OfflineHeuristicModel()


def build_services(environ: Mapping[str, str] | None = None, settings: Settings | None = None) -> Services:
    env = os.environ if environ is None else environ
    settings = settings or get_settings()
    notes: dict[str, str] = {}

    # --- operational persistence (a failure here is fatal for the API: nothing can be stored)
    database = OperationsDatabase(database_path_from_url(env.get("DATABASE_URL")))
    database.migrate()
    store = OperationsStore(database)
    swept = store.sweep_interrupted_investigations()
    if swept:
        logger.warning("marked %d interrupted investigation(s) as failed", swept)

    # --- authentication (the Phase 7 verifier, reused as is)
    mode = (env.get("APPROVAL_AUTH_MODE") or "disabled").strip().lower()
    try:
        authenticator: Authenticator = build_authenticator(env)
        auth_status = mode if mode in ("disabled", "dev_static") else "misconfigured"
    except ApprovalConfigError:
        authenticator, auth_status = DenyAllAuthenticator(), "misconfigured"  # fail closed: nobody authenticates

    # --- investigations (existing agent; deterministic offline by default)
    inv_mode = (env.get("INVESTIGATION_MODE") or "offline").strip().lower()
    if inv_mode not in ("offline", "live"):
        inv_mode = "offline"
        notes["investigation_mode"] = "invalid value ignored; using offline"
    agent_config = load_agent_config()
    investigations = InvestigationService(store, llm_factory=_llm_factory(inv_mode, agent_config), agent_config=agent_config, mode=inv_mode)

    # --- tickets
    ticket_status, adapter = "not_configured", None
    try:
        gh = GitHubConfig.from_environ(env)
        if gh is not None:
            adapter, ticket_status = GitHubIssuesAdapter(gh), "configured"
    except ValueError:
        ticket_status = "misconfigured"
    tickets = TicketService(store, adapter)

    services = Services(store=store, authenticator=authenticator, investigations=investigations, tickets=tickets,
                        app_env=settings.app_env, auth_status=auth_status, ticket_status=ticket_status, notes=notes)

    # --- governance + approval + recovery (each step may disable recovery, with a reason)
    try:
        recorder = GovernanceRecorder(open_ledger(env.get("GOVERNANCE_LEDGER_PATH") or None))
        signer = ApprovalSigner.from_environ(env)
        approvals = ApprovalService(SqliteApprovalStore(env.get("APPROVAL_STORE_PATH") or None), signer, authenticator, recorder)
        services.recorder, services.approvals = recorder, approvals
    except Exception as exc:  # noqa: BLE001 - approvals/recovery stay off; the reason is a type name, never a value
        services.recovery_unavailable = "approvals_not_configured:" + type(exc).__name__
        return services
    try:
        from app.db.connection import make_engine
        from app.db.repository import SqlRepository
        from app.simulation.runlog import RunLogStore
        from app.simulation.service import SimulationService

        sim = SimulationService(SqlRepository(make_engine(settings)), RunLogStore(Path(settings.sim_log_dir)),
                                seed=settings.sim_seed, freshness_threshold_minutes=settings.freshness_threshold_minutes)
        executor = RecoveryExecutor(sim)  # refuses any database that is not a *_test database (dataops_sim included)
        governed = GovernedActionService(recorder, RecoveryRunner(executor, approvals, recorder), policy=build_policy(RECOVERY_ACTIONS))
        services.recoveries = RecoveryService(store, RecoveryWorkflow(approvals, governed, recorder), approvals, recorder)
        services.recovery_unavailable = None
        services.recoveries.reconcile_all()
    except Exception as exc:  # noqa: BLE001
        services.recovery_unavailable = "recovery_target_unavailable:" + type(exc).__name__
    return services
