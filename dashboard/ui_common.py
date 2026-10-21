"""Small Streamlit helpers shared by both apps (presentation only)."""

from __future__ import annotations

import streamlit as st

from dashboard.http_core import Problem

HINTS = {
    "unauthenticated": "The token was rejected or is missing. Set the token environment variable for this app and restart it.",
    "forbidden": "This token's role is not allowed to read this data.",
    "unreachable": "The API could not be reached. Check that it is running and that DASHBOARD_API_BASE_URL is correct.",
    "timeout": "The API took too long to answer. Try Refresh; nothing was changed.",
    "unavailable": "The API reports this capability as unavailable in its current configuration.",
    "rate_limited": "Too many failed authentication attempts. Wait before retrying.",
    "bad_response": "The API answered with something unexpected.",
}

# colour per meaning; every badge also carries its text, so colour is never the only signal
SEVERITY_BADGE = {"critical": "red", "high": "orange", "medium": "yellow", "low": "blue"}
STATUS_BADGE = {"open": "red", "new": "blue", "investigating": "orange", "awaiting_review": "violet", "awaiting_approval": "violet",
                "resolved": "green", "closed": "gray", "recovery_completed": "green", "recovery_uncertain": "red",
                "recovery_failed": "red", "recovery_not_executed": "gray", "pending": "violet", "approved": "green",
                "rejected": "gray", "expired": "gray"}

_CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1400px;}
[data-testid="stMetric"] {background: rgba(128,128,128,.08); border-radius: .6rem; padding: .7rem .9rem;}
.demo-banner {border-left: .35rem solid #7c3aed; background: rgba(124,58,237,.10); padding: .5rem .8rem; border-radius: .3rem; margin-bottom: .6rem;}
a:focus-visible, button:focus-visible {outline: 3px solid #2563eb !important; outline-offset: 2px;}
</style>
"""


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def badge(kind: str, value: object) -> str:
    """Markdown badge, e.g. ``:red-badge[critical]``. Falls back to plain text for an unknown value."""
    text = str(value if value not in (None, "") else "unknown").replace("[", "(").replace("]", ")")
    colour = (SEVERITY_BADGE if kind == "severity" else STATUS_BADGE).get(str(value))
    return f":{colour}-badge[{text}]" if colour else f":gray-badge[{text}]"


def demo_banner(text: str = "SYNTHETIC DEMONSTRATION DATA - not real incidents, not stored in any real system.") -> None:
    st.markdown(f'<div class="demo-banner" role="note"><strong>DEMO</strong> &nbsp;{text}</div>', unsafe_allow_html=True)


def nav_links(*, to_ledger: str | None = None, to_ops: str | None = None) -> None:
    """Links to the sibling app, in the sidebar. A deep link carries only identifiers, never a token."""
    if to_ledger:
        st.sidebar.link_button("Open VerbaLedger Explorer", to_ledger, width="stretch", icon=":material/fact_check:")
    if to_ops:
        st.sidebar.link_button("Open Operations Dashboard", to_ops, width="stretch", icon=":material/monitoring:")


def show_problem(problem: Problem, what: str) -> None:
    extra = f" (HTTP {problem.status}, code {problem.code}, request {problem.request_id})" if problem.status else ""
    st.error(f"Could not load {what}: {problem.message}{extra}")
    hint = HINTS.get(problem.kind)
    if hint:
        st.caption(hint)


def show_recovery_guidance(guide: dict) -> None:
    """The honest 'this capability is off, here is exactly why and how to enable it' state. Not an error to hide, not a
    working page: the queue is not drawn and no count is shown."""
    st.warning(f"**{guide['title']}.** {guide['message']}")
    bits = [f"reason: {guide['reason']}" if guide.get("reason") else None,
            f"database: {guide['database']}" if guide.get("database") else None,
            f"request {guide['request_id']}" if guide.get("request_id") else None]
    st.caption("API said - " + "; ".join(b for b in bits if b) + ". Nothing is pending or decided here until this is fixed.")
    if guide.get("steps"):
        with st.expander("How to enable Human Review and recovery (Windows PowerShell)", expanded=True):
            st.markdown("The safety guard is deliberate and stays on: recovery is only allowed to write to a database whose name ends with "
                        "`_test`, never to `dataops_sim`. Use a separate demo database:")
            st.code("\n".join(guide["steps"]), language="powershell")
    elif guide.get("fix"):
        st.info(guide["fix"])


def show_chart(container, fig, empty_message: str) -> None:
    """Draw a chart, or an explanatory note when there is nothing to draw. A real statement on purpose: a bare conditional
    expression would be 'magic'-rendered by Streamlit as the text of a DeltaGenerator object."""
    if fig:
        container.plotly_chart(fig, width="stretch")
    else:
        container.info(empty_message)


def tone(kind: str, text: str) -> None:
    {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(text)


def pager(total: int, page: int, page_count: int, noun: str) -> None:
    st.caption(f"{total} {noun} match - page {page} of {page_count}")
