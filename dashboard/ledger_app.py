"""VerbaLedger Explorer (Streamlit). Run:  streamlit run dashboard/ledger_app.py --server.port 8502

A separate, READ-ONLY application with its own identity, navigation and token. It talks only to ``/api/v1/ledger/*``
and has no method that could append to, modify, repair or truncate the ledger, clear a suspension, approve or
execute a recovery, change policy, or re-enable an agent. It accepts no filesystem path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run dashboard/<app>.py` puts only this folder on sys.path, not the project root, so the `dashboard`
# package would not be importable. Add the project root (this file's grandparent) once, before the package imports.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

from dashboard import config, ledger_plots as charts, ledger_data as ld, runtime
from dashboard.ui_common import apply_theme, badge, nav_links, pager, show_chart, show_problem, tone

PAGES = ("Overview", "Events", "Causal trace", "Integrity verification", "Semantic audit", "About this explorer")

st.set_page_config(page_title="VerbaLedger Explorer", page_icon=":material/fact_check:", layout="wide")
client = runtime.ledger_client()
apply_theme()

# Deep links from the Operations Dashboard: ?page=...&incident_id=...&request_id=...&decision_id=... (identifiers only)
if "deep_link_done" not in st.session_state:
    st.session_state["deep_link_done"] = True
    _qp = st.query_params
    if _qp.get("page") in PAGES:
        st.session_state["page"] = _qp.get("page")
    if _qp.get("incident_id"):
        st.session_state["f_incident"] = str(_qp.get("incident_id"))[:128]
    if _qp.get("request_id"):
        st.session_state["f_request"] = str(_qp.get("request_id"))[:128]
        st.session_state["trace_kind"], st.session_state["trace_value"] = "request_id", str(_qp.get("request_id"))[:128]
    if _qp.get("decision_id"):
        st.session_state["trace_kind"], st.session_state["trace_value"] = "decision_id", str(_qp.get("decision_id"))[:128]

st.sidebar.title("VerbaLedger Explorer")
st.sidebar.caption("Read-only view of the governance ledger")
nav_links(to_ops=config.ops_dashboard_url())
page = st.sidebar.radio("Page", PAGES, key="page")
refresh = st.sidebar.button("Refresh ledger view", key="refresh", help="Re-read the ledger file instead of using the cached snapshot.")
_side = client.summary(refresh=refresh)
if _side.ok:
    st.sidebar.metric("Governance events recorded", _side.data["total_records"], help="Real records read from the persisted ledger. Never synthetic.")
st.sidebar.caption(f"API: {client.base_url}")
st.sidebar.caption("Token: " + ("configured" if client.has_token else "NOT configured"))


def show_banner(meta: dict) -> None:
    kind, lines = ld.banner(meta)
    for line in lines:
        (st.warning if kind == "warning" and line.startswith(("INCOMPLETE", "The ledger changed")) else st.caption)(line)


def iso(day, end: bool = False) -> str | None:
    if day is None:
        return None
    return datetime.combine(day, time.max if end else time.min, tzinfo=timezone.utc).isoformat()


# --------------------------------------------------------------------------------------------- overview
if page == "Overview":
    st.header("Overview")
    st.caption("Every event here is a real record read from the governance ledger. This explorer never creates, imports or displays "
               "synthetic ledger records; the synthetic demo incidents live only in the Operations Dashboard and have no ledger events.")
    summary = client.summary(refresh=refresh)
    if not summary.ok:
        show_problem(summary.problem, "the ledger summary")
    else:
        s = summary.data
        show_banner(s["view"])
        if s["total_records"] == 0:
            st.info("The ledger has no readable records yet. Events appear here once the governed workflow records them.")
        else:
            cards = ld.overview_cards(s)
            for col, (label, value, note) in zip(st.columns(len(cards)), cards):
                col.metric(label, value, help=note)
            st.caption("Allowed / denied / suspended / human-review counts group recorded outcomes by name (classes can overlap). "
                       f"Outcomes outside those groups: {s['decisions']['other_outcomes']}. Grouping: " + "; ".join(f"{k} = {', '.join(v)}" for k, v in s["decision_classes"].items()))
            c1, c2 = st.columns(2)
            fig = charts.events_over_time_chart(ld.day_pairs(s))
            show_chart(c1, fig, "No dated events.")
            fig2 = charts.entry_type_chart(s["by_entry_type"])
            show_chart(c2, fig2, "No entry types.")
            st.metric("Automation suspended", {True: "yes", False: "no", None: "unknown"}[s.get("suspended")])
            latest_text = ld.latest_event_text(s)
            if latest_text:
                st.caption(latest_text)
            with st.expander("Events by incident"):
                pairs = ld.incident_pairs(s)
                if pairs:
                    st.dataframe(pd.DataFrame(pairs, columns=["incident_id", "events"]), hide_index=True, width="stretch")
                    st.caption("Counts are of persisted ledger records that name the incident. Use Events > Incident id to open them.")
                else:
                    st.write("No recorded event names an incident yet.")
        st.subheader("Integrity status")
        if st.button("Verify hash chain now", key="overview_integrity"):
            st.session_state["integrity"] = client.integrity()
        ires = st.session_state.get("integrity")
        if ires is None:
            st.info("Not verified in this session. Press the button to recompute the hash-chain check (read-only).")
        elif not ires.ok:
            show_problem(ires.problem, "the integrity verification")
        else:
            iv = ld.integrity_view(ires.data)
            tone(iv["tone"], iv["heading"])
            st.caption(f"Verified at {ires.data.get('verified_at')}. A valid chain does not prove no trailing events were removed.")

# --------------------------------------------------------------------------------------------- events
elif page == "Events":
    st.header("Governance events")
    summary = client.summary(refresh=refresh)
    if not summary.ok:
        show_problem(summary.problem, "the ledger summary")
    else:
        s = summary.data
        show_banner(s["view"])
        c1, c2, c3 = st.columns(3)
        c1.metric("Records readable", s["total_records"])
        c2.metric("Automation suspended", {True: "yes", False: "no", None: "unknown"}[s.get("suspended")])
        c3.metric("Time span", f"{(s['first_time'] or '-')[:10]} to {(s['last_time'] or '-')[:10]}")
        if s["total_records"] == 0:
            st.info("The ledger has no readable records.")
        else:
            with st.form("filters"):
                a, b, c = st.columns(3)
                f_from = a.date_input("From (UTC date)", value=None)
                f_to = b.date_input("To (UTC date)", value=None)
                f_type = c.selectbox("Event type", ["(any)", *ld.ENTRY_TYPES])
                d1, d2, d3 = st.columns(3)
                f_decision = d1.text_input("Decision id", key="f_decision")
                f_incident = d2.text_input("Incident id", key="f_incident")
                f_request = d3.text_input("Request / recovery id", key="f_request")
                f_text = st.text_input("Search text (ids, outcome, action, rule, type)", key="f_text", max_chars=100)
                e1, e2, e3 = st.columns(3)
                f_outcome = e1.selectbox("Outcome", ["(any)", *sorted(s["by_outcome"])])
                f_action = e2.selectbox("Action", ["(any)", *sorted(s["by_action"])])
                f_rule = e3.selectbox("Spec / rule id", ["(any)", *sorted(s["by_rule_id"])])
                g1, g2, g3 = st.columns(3)
                order = g1.selectbox("Order", ["desc", "asc"], format_func=lambda x: "newest first" if x == "desc" else "oldest first")
                size = g2.selectbox("Page size", ld.PAGE_SIZES, index=1)
                page_no = g3.number_input("Page", min_value=1, value=1, step=1)
                st.form_submit_button("Apply filters")
            pick = lambda v: None if v in ("(any)", "") else v.strip()  # noqa: E731
            res = client.events(limit=int(size), offset=(int(page_no) - 1) * int(size), order=order, refresh=refresh,
                                from_time=iso(f_from), to_time=iso(f_to, end=True), entry_type=pick(f_type),
                                decision_id=pick(f_decision), incident_id=pick(f_incident), request_id=pick(f_request),
                                outcome=pick(f_outcome), action=pick(f_action), rule_id=pick(f_rule), q=pick(f_text))
            if not res.ok:
                show_problem(res.problem, "events")
            elif not res.data["items"]:
                st.info("No events match these filters." if res.data["total"] == 0 else "This page is past the last result.")
                if pick(f_incident):
                    st.caption(f"Events recorded for incident {pick(f_incident)}: {res.data['total']}. Events appear after the incident goes "
                               "through the governed workflow (investigation, human review, recovery request).")
            else:
                total = res.data["total"]
                pager(total, int(page_no), max(1, -(-total // int(size))), "events")
                if pick(f_incident):
                    st.metric(f"Events for incident {pick(f_incident)}", total)
                _iv = st.session_state.get("integrity")
                st.caption("Integrity: " + ("not verified in this session (use 'Verify hash chain now' on Overview or the Integrity page)."
                                            if _iv is None else ("hash chain verified at " + str(_iv.data.get("verified_at")) if _iv.ok and _iv.data.get("status") == "verified" else "see the Integrity page: the last check was not clean or failed.")))
                st.dataframe(pd.DataFrame(ld.event_rows(res.data["items"])), hide_index=True, width="stretch")
                chosen = st.selectbox("Event detail", [i["entry_id"] for i in res.data["items"]], key="event_pick",
                                      format_func=lambda x: next(f"#{i['sequence']} {i['entry_type']} {i.get('outcome') or ''} ({x[:8]})"
                                                                 for i in res.data["items"] if i["entry_id"] == x))
                det = client.event(chosen)
                if not det.ok:
                    show_problem(det.problem, "the event")
                else:
                    ev = det.data["event"]
                    st.subheader(f"#{ev['sequence']} {ev['entry_type']} - {ev['time']}")
                    st.write(f"**Entry id:** `{ev['entry_id']}`  **Caused by:** `{ev['caused_by'] or 'nothing recorded'}`  **Decision:** `{ev['decision_id'] or '-'}`")
                    details = ld.vsl_details(ev)
                    if details:
                        st.write("**Recorded VSL details**")
                        for label, value in details:
                            st.write(f"- {label}: {value}")
                    else:
                        st.caption("No VSL decision fields are recorded in this event's payload.")
                    if ev["payload_truncated"]:
                        st.warning("The payload was too large and is shown truncated.")
                    st.json(ev["payload"], expanded=False)
                    if ev.get("incident_id"):
                        st.link_button("Open this incident in the Operations Dashboard", f"{config.ops_dashboard_url()}/?page=Incidents&incident={ev['incident_id']}")
                    if ev.get("request_id"):
                        st.link_button("Open the human review for this request", f"{config.ops_dashboard_url()}/?page=Human+Review&review={ev['request_id']}")
                    st.caption(f"prev_hash `{ev['prev_hash']}`  entry_hash `{ev['entry_hash']}`  line {ev['line']}")

# --------------------------------------------------------------------------------------------- trace
elif page == "Causal trace":
    st.header("Causal trace")
    st.caption("Edges are ONLY the recorded caused_by links. Identifiers choose which records to include; time order is "
               "shown separately and is never treated as causality.")
    kind = st.radio("Trace by", ["decision_id", "request_id", "entry_id"], horizontal=True, key="trace_kind")
    value = st.text_input("Identifier", key="trace_value", max_chars=128)
    if value.strip():
        res = client.trace(**{kind: value.strip()}, refresh=refresh)
        if not res.ok:
            if res.problem.code == "ledger_trace_not_found":
                st.info("No recorded events match that identifier." + (" The ledger view is incomplete, so it may exist in unreadable data." if res.problem.details.get("view_complete") is False else ""))
            else:
                show_problem(res.problem, "the trace")
        else:
            t = res.data
            show_banner(t["view"])
            for level, text in ld.trace_flags(t):
                tone(level, text)
            st.subheader("Lifecycle stages")
            st.caption("Only stages with a matching record in THIS trace are marked recorded. A missing stage is shown as missing, never inferred.")
            st.dataframe(pd.DataFrame([{"stage": x["label"], "status": "recorded" if x["recorded"] else "NOT RECORDED",
                                        "records": ", ".join(f"#{e['sequence']} {e['outcome']}" for e in x["events"]) or "-",
                                        "note": x["note"] or ""} for x in ld.lifecycle_stages(t)]), hide_index=True, width="stretch")
            incident_ids = sorted({n.get("incident_id") for n in t["nodes"] if n.get("incident_id")})
            request_ids = sorted({n.get("request_id") for n in t["nodes"] if n.get("request_id")})
            for iid in incident_ids[:3]:
                st.link_button(f"Open incident {iid} in the Operations Dashboard", f"{config.ops_dashboard_url()}/?page=Incidents&incident={iid}")
            for rid in request_ids[:3]:
                st.link_button(f"Open review for request {rid}", f"{config.ops_dashboard_url()}/?page=Human+Review&review={rid}")
            st.graphviz_chart(ld.trace_dot(t), width="stretch")
            st.subheader("Records (ledger order)")
            st.dataframe(pd.DataFrame(ld.event_rows(t["nodes"])), hide_index=True, width="stretch")
            st.subheader("Recorded causal links")
            st.write("\n".join(f"- `{e['cause'][:8]}` caused `{e['effect'][:8]}` (caused_by)" for e in t["edges"]) or "No recorded cause links among these records.")
            st.subheader("Chronology (time order - not causal evidence)")
            st.write(" -> ".join(f"`{x[:8]}`" for x in t["chronology"]))
            for n in t["notes"]:
                st.caption(n)
    else:
        st.info("Enter a decision id, request id or entry id to see its recorded trace.")

# --------------------------------------------------------------------------------------------- integrity
elif page == "Integrity verification":
    st.header("Integrity verification")
    st.caption("Cryptographic hash-chain check, recomputed each time you run it. It reads the ledger and never repairs or changes it.")
    if st.button("Run integrity verification", key="run_integrity"):
        st.session_state["integrity"] = client.integrity()
    res = st.session_state.get("integrity")
    if res is None:
        st.info("Not run yet in this session.")
    elif not res.ok:
        show_problem(res.problem, "the integrity verification")
        st.warning("Verification could not complete.")
    else:
        v = ld.integrity_view(res.data)
        tone(v["tone"], v["heading"])
        for line in v["lines"]:
            st.write(line)
        if v["checkpoint"]:
            st.caption(f"Last verified record: sequence {v['checkpoint']['sequence']}, hash {v['checkpoint']['entry_hash']}")
        st.caption("This result is a point-in-time check; run it again for a current one.")
    with st.expander("What this does NOT prove"):
        from_api = (res.data.get("limits") if res is not None and res.ok else None) or [
            "A valid hash chain cannot detect events removed from the end of the file.",
            "It cannot detect replacement by an older internally consistent chain.",
            "It says nothing about whether the recorded events are semantically correct."]
        st.write("\n".join(f"- {x}" for x in from_api))

# --------------------------------------------------------------------------------------------- audit
elif page == "Semantic audit":
    st.header("Semantic audit")
    st.caption("Do the recorded events satisfy the VSL ordering rules? This is separate from cryptographic integrity.")
    use_gap = st.checkbox("Also check monitoring gaps", key="use_gap")
    gap = st.number_input("Maximum seconds between monitor records", min_value=1.0, value=3600.0, key="gap", disabled=not use_gap)
    if st.button("Run semantic audit", key="run_audit"):
        st.session_state["audit"] = client.audit(max_monitor_gap_seconds=float(gap) if use_gap else None)
    res = st.session_state.get("audit")
    if res is None:
        st.info("Not run yet in this session.")
    elif not res.ok:
        show_problem(res.problem, "the audit")
    else:
        v = ld.audit_view(res.data)
        tone(v["tone"], v["heading"])
        if v["error"]:
            st.error(v["error"])
        if v["rows"]:
            st.dataframe(pd.DataFrame(v["rows"]), hide_index=True, width="stretch")
        st.caption(v["integrity_note"])
        for check in res.data["checks"]:
            with st.expander(f"{check['title']} - {check['status'].replace('_', ' ')}"):
                st.write(check["explanation"])
                if check["status"] == "not_run":
                    st.info("Not run. Enable the monitoring gap option to run it." if check["name"] == "no_monitoring_gaps" else "Not run.")
                if check["violations"]:
                    st.error("Violations: " + ", ".join(str(x) for x in check["violations"]))
                    st.dataframe(pd.DataFrame(ld.event_rows(check["events"])), hide_index=True, width="stretch")
        for n in v["notes"]:
            st.caption(n)

# --------------------------------------------------------------------------------------------- about
else:
    st.header("About this explorer")
    st.write("The VerbaLedger Explorer is read-only. It cannot append, modify, delete, repair or truncate ledger events, clear "
             "a suspension, approve or execute a recovery, change policy, or re-enable an agent. Those actions exist only "
             "in the governed workflow and the human governance authority path.")
    st.write("It reads the ledger through `/api/v1/ledger/*` with its own token (permission `ledger:read`). It never accepts "
             "a file path, and unreadable lines are always reported rather than skipped silently.")
    st.write("The hash chain proves internal consistency only. It cannot detect truncated trailing events or whether "
             "the recorded events are semantically right. The semantic audit checks VSL ordering rules, and also cannot "
             "detect removed trailing events.")
