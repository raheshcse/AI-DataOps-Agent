"""Phase 7: human approval and controlled (simulated) recovery. See docs/phase7-human-approval-recovery.md."""

from app.recovery.actions import ACTIONS, RECOVERY_ACTIONS, ActionRejected, RecoveryExecutor, validate_action
from app.recovery.approvals import ApprovalDenied, ApprovalService, ApprovalSigner, SqliteApprovalStore
from app.recovery.auth import (
    ApprovalConfigError,
    DenyAllAuthenticator,
    Identity,
    StaticTokenAuthenticator,
    build_authenticator,
)
from app.recovery.reenable import ReEnablementService
from app.recovery.runner import RecoveryRunner
from app.recovery.workflow import (
    ProposedRecovery,
    RecoveryOutcome,
    RecoveryWorkflow,
    proposal_from_report,
    suggest_recovery,
)

__all__ = ["ACTIONS", "RECOVERY_ACTIONS", "ActionRejected", "ApprovalConfigError", "ApprovalDenied", "ApprovalService",
           "ApprovalSigner", "DenyAllAuthenticator", "Identity", "ProposedRecovery", "ReEnablementService",
           "RecoveryExecutor", "RecoveryOutcome", "RecoveryRunner", "RecoveryWorkflow", "SqliteApprovalStore",
           "StaticTokenAuthenticator", "build_authenticator", "proposal_from_report", "suggest_recovery",
           "validate_action"]
