"""Ticket association for incidents (GitHub Issues provider).

* The internal incident is never lost when the provider is down: the failure is recorded and the call can be retried.
* No ticket id/URL is ever invented: an association only gets external identifiers from a real provider response.
* Duplicate prevention is layered: one local row per incident (a unique key), a claim before talking to the provider,
  and a lookup by the incident marker in the issue body before creating (this also recovers from a create whose
  outcome is unknown, e.g. a timeout).
* A ticket's open/closed state is information only. It never changes an incident's investigation state, a
  recovery, an approval or governance.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.integrations import GitHubIssuesAdapter, Issue, TicketError, incident_marker
from app.persistence import OperationsStore


class TicketsUnavailable(Exception):
    """Ticketing is not configured. Nothing is fabricated."""


class TicketBusy(Exception):
    """Another request is already creating this incident's ticket."""


class NoTicket(Exception):
    pass


_UNCONFIRMED = {"timeout", "unavailable", "service_error", "bad_response"}


def build_issue(incident: dict[str, Any], investigation: dict[str, Any] | None) -> tuple[str, str]:
    title = f"[DataOps][{incident['severity']}] {incident['title']}"[:200]
    lines = [
        f"Incident: `{incident['incident_id']}`",
        f"External reference: {incident['external_ref'] or 'none'}",
        f"Type: {incident['incident_type']}   Severity: {incident['severity']}",
        "",
        "This issue was opened by the AI DataOps Agent. Its open/closed state does not authorise or prove any recovery.",
    ]
    if investigation and investigation.get("report_status"):
        lines += ["", f"Latest investigation `{investigation['investigation_id']}`: {investigation['report_status']}",
                  (investigation.get("summary") or "")[:1000]]
    lines += ["", incident_marker(incident["incident_id"])]
    return title, "\n".join(lines)


class TicketService:
    def __init__(self, store: OperationsStore, adapter: GitHubIssuesAdapter | None) -> None:
        self.store, self.adapter = store, adapter

    @property
    def configured(self) -> bool:
        return self.adapter is not None

    async def create_for_incident(self, incident: dict[str, Any], investigation: dict[str, Any] | None
                                  ) -> tuple[dict[str, Any], bool]:
        """Returns (association, created_now). A second call returns the existing association (created_now=False)."""
        iid = incident["incident_id"]
        if self.adapter is None:
            self.store.add_ticket_event(iid, "create", "unavailable", "not_configured")
            raise TicketsUnavailable()
        assoc, claimed = self.store.begin_ticket(incident_id=iid, provider=self.adapter.provider, repository=self.adapter.repository)
        if not claimed:
            if assoc["status"] == "creating":
                raise TicketBusy()
            return assoc, False
        title, body = build_issue(incident, investigation)
        adopted = False
        try:
            issue: Issue | None = await asyncio.to_thread(self.adapter.find_issue_by_marker, incident_marker(iid))
            if issue is None:
                try:
                    issue = await asyncio.to_thread(self.adapter.create_issue, title=title, body=body)
                except TicketError as exc:
                    if exc.kind in _UNCONFIRMED:  # it may or may not exist: the next attempt must look before creating
                        raise TicketError(exc.kind, retry_after=exc.retry_after, unconfirmed=True) from exc
                    raise
            else:
                adopted = True
        except TicketError as exc:
            self.store.finish_ticket(iid, status="creation_unconfirmed" if exc.unconfirmed else "creation_failed", sync_result=exc.kind)
            self.store.add_ticket_event(iid, "create", "failed", exc.kind)
            raise
        except BaseException:
            self.store.finish_ticket(iid, status="creation_unconfirmed", sync_result="interrupted")
            raise
        saved = self.store.finish_ticket(iid, status=issue.state, external_id=issue.external_id, external_number=issue.number,
                                         url=issue.url, sync_result="ok")
        self.store.add_ticket_event(iid, "create", "adopted_existing" if adopted else "created")
        return saved, not adopted

    def get(self, incident_id: str) -> dict[str, Any] | None:
        return self.store.get_ticket(incident_id)

    async def sync(self, incident_id: str) -> dict[str, Any]:
        if self.adapter is None:
            self.store.add_ticket_event(incident_id, "sync", "unavailable", "not_configured")
            raise TicketsUnavailable()
        assoc = self.store.get_ticket(incident_id)
        if assoc is None or assoc["external_number"] is None or assoc["status"] not in ("open", "closed"):
            raise NoTicket()
        try:
            issue = await asyncio.to_thread(self.adapter.get_issue, assoc["external_number"])
        except TicketError as exc:
            self.store.finish_ticket(incident_id, status=assoc["status"], sync_result=exc.kind)  # keep the last known state
            self.store.add_ticket_event(incident_id, "sync", "failed", exc.kind)
            raise
        saved = self.store.finish_ticket(incident_id, status=issue.state, sync_result="ok")
        self.store.add_ticket_event(incident_id, "sync", "synced")
        return saved
