"""External integrations (Phase 8). Currently: GitHub Issues."""

from app.integrations.github_issues import (
    GitHubConfig,
    GitHubIssuesAdapter,
    Issue,
    TicketError,
    incident_marker,
)

__all__ = ["GitHubConfig", "GitHubIssuesAdapter", "Issue", "TicketError", "incident_marker"]
