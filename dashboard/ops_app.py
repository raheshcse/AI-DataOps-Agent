"""Operations Dashboard (Streamlit). Run:  streamlit run dashboard/ops_app.py

Reads the existing FastAPI ``/api/v1`` API only. It cannot write to SQLite, the ledger or SQL Server, never retries or
resumes a recovery, and relays an approval decision only through the authenticated approvals endpoint (the approver's
own token, typed into a masked field, used once and not kept).
"""

from __future__ import annotations

import sys
from pathlib import Path

# `streamlit run dashboard/<app>.py` puts only this folder on sys.path, not the project root, so the `dashboard`
# package would not be importable. Add the project root (this file's grandparent) once, before the package imports.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import time

import pandas as pd
import streamlit as st

from dashboard import charts, config, ops_data as d, runtime
from dashboard.ui_common import apply_theme, badge, demo_banner, nav_links, pager, show_chart, show_problem, show_recovery_guidance, tone

PAGES = ("Overview", "Incidents", "Investigations", "Human Review", "Recovery & approvals", "Pipelines & runs", "Tickets", "System status")
SRC_WF, SRC_SIM, SRC_DEMO = "Workflow incidents", "SQL Server simulator", "Synthetic demo (100)"
ANY = "any"

st.set_page_config(page_title="DataOps Operations Dashboard", page_icon=":material/monitoring:", layout="wide")
client = runtime.ops_client()
apply_theme()

# Deep links from the VerbaLedger Explorer: ?page=...&incident=...&review=... (identifiers only, never a token)
if "deep_link_done" not in st.session_state:
    st.session_state["deep_link_done"] = True
    _qp = st.query_params
    if _qp.get("page") in PAGES:
        st.session_state["page"] = _qp.get("page")
    for _name in ("incident", "review"):
        if _qp.get(_name):
            st.session_state["focus_" + _name] = str(_qp.get(_name))[:64]  # a review link may carry a recovery id or an approval request id


def loaded(force: bool = False) -> d.LoadedIncidents:
    if force or "loaded" not in st.session_state:
        st.session_state["loaded"] = d.load_incidents(client, max_items=config.max_incidents())
    return st.session_state["loaded"]


def invalidate_loaded() -> None:
    """Drop the cached incident list after any action that may change workflow state (import, investigation, recovery
    request, human decision), so the next render shows the real state instead of the pre-action snapshot."""
    st.session_state.pop("loaded", None)


def freshness(data: d.LoadedIncidents) -> None:
    bits = [f"Loaded {len(data.items)} of {data.total if data.total is not None else '?'} incidents at {data.loaded_at}"]
    if data.truncated:
        bits.append("older incidents are not loaded (raise DASHBOARD_MAX_INCIDENTS to load more)")
    st.caption(" - ".join(bits))
    if data.partial:
        st.warning("The incident list is only partially loaded because a later request failed.")


# --------------------------------------------------------------------------------------------- sidebar
st.sidebar.title("DataOps Operations")
st.sidebar.caption("Operations Dashboard - read-only except approval decisions, starting investigations and importing simulator incidents via the API")
nav_links(to_ledger=config.ledger_explorer_url())
page = st.sidebar.radio("Page", PAGES, key="page")
if st.sidebar.button("Refresh data", key="refresh"):
    for key in ("loaded", "decision_result", "submitted"):
        st.session_state.pop(key, None)
st.sidebar.caption(f"API: {client.base_url}")
st.sidebar.caption("Token: " + ("configured" if client.has_token else "NOT configured"))
data = loaded()
if not data.available and data.problem is not None and page != "System status":
    show_problem(data.problem, "incidents")

by_id = {i["incident_id"]: i for i in data.items}


def pick_incident(label: str, key: str, ids: list[str]) -> str | None:
    if not ids:
        return None
    focus = st.session_state.get("focus_incident")
    if focus in ids and key not in st.session_state:
        st.session_state[key] = focus  # arrived from a deep link
    return st.selectbox(label, ids, key=key, format_func=lambda x: f"{x} - {by_id.get(x, {}).get('title', '')[:70]}")


def decision_panel(rid: str) -> None:
    """Decision form shared by Human Review and Recovery. The decision is relayed once, with the approver's own token."""
    if rid in st.session_state.get("submitted", {}):
        st.warning("A decision was already submitted for this request in this session. Press 'Refresh data' and check "
                   "the status before doing anything else.")
    else:
        st.subheader("Decide")
        st.caption("Your decision is relayed to the existing approvals endpoint with YOUR token. The server checks your "
                   "permission for this action; nothing is executed by this page.")
        with st.form("decision_form", clear_on_submit=True):
            decision = st.radio("Decision", ["approve", "reject"], horizontal=True, key="decision_choice")
            reason = st.text_input("Reason (required)", max_chars=500, key="decision_reason")
            approver_token = st.text_input("Your approver token", type="password", key="decision_token")
            confirm = st.checkbox("I reviewed the action, target, risk and expiry above", key="decision_confirm")
            submitted = st.form_submit_button("Submit decision", key="decision_submit")
        if submitted:
            if not (reason.strip() and approver_token and confirm):
                st.error("A reason, your token and the confirmation are all required.")
            else:
                result = client.decide(rid, decision, reason.strip(), approver_token)  # sent once, never retried
                approver_token = ""  # not kept
                st.session_state.setdefault("submitted", {})[rid] = True
                st.session_state["decision_result"] = d.decision_outcome_text(result)
                invalidate_loaded()
                st.rerun()  # re-render from fresh state; the outcome is shown from session state


def render_report(detail: dict) -> None:
    """One investigation, with explicit labels for observed evidence versus unconfirmed hypotheses."""
    view = d.report_view(detail)
    tn, headline = d.investigation_progress(detail)
    tone(tn, headline)
    st.write(f"**Status:** {view['status']} - **Outcome:** {view['report_status'] or 'none'}")
    if view["error"]:
        st.error(f"Investigation error: {view['error'].get('kind')}")
    if not view["has_report"]:
        st.info("No report is available for this investigation yet.")
        return
    st.write(view["summary"])
    st.subheader(view["cause_label"])
    st.write(view["cause_statement"] or view["cause_explanation"])
    st.caption(f"Confidence: {view['confidence']} - {view['confidence_explanation']}")
    st.subheader("Observed findings")
    for s in view["observed"] or ["none recorded"]:
        st.write(f"- {s}")
    st.subheader(view["hypothesis_label"])
    for s in view["hypotheses"] or ["none recorded"]:
        st.write(f"- {s}")
    with st.expander("Evidence"):
        st.dataframe(pd.DataFrame(view["evidence"] or [{"id": "none"}]), hide_index=True, width="stretch")
    with st.expander("Recommended next steps and limitations"):
        st.write("\n".join(f"- {s}" for s in view["next_steps"]) or "none")
        st.write("\n".join(f"- {s}" for s in view["limitations"]) or "none")


def import_panel(sim_id: str, workflow_id: str | None) -> None:
    """Explicit, idempotent import of one simulator incident into the workflow store (operations store only)."""
    st.subheader("Import into the workflow store")
    notice = st.session_state.get("import_notice")
    if notice and notice[0] == sim_id:
        tone(notice[1], notice[2])
    if workflow_id:
        if not (notice and notice[0] == sim_id and notice[1] == "success"):
            st.success(f"Imported as workflow incident {workflow_id}. Find it under 'Workflow incidents', then investigate it.")
        return
    st.caption("Creates one workflow incident from this simulator record. The simulator record is not changed, and nothing is "
               "investigated, approved or recovered by importing.")
    env_token = config.investigate_token()
    with st.form(f"import_form_{sim_id}", clear_on_submit=True):
        typed = "" if env_token else st.text_input("Your token (needs incident:create)", type="password", key=f"imp_token_{sim_id}")
        clicked = st.form_submit_button("Import into workflow store", key=f"imp_go_{sim_id}")
    if clicked:
        token = env_token or typed
        if not token:
            st.session_state["import_notice"] = (sim_id, "error", "A token that may import incidents is required. Nothing was imported.")
        else:
            tn, text, _ = d.import_outcome(client.import_simulator_incident(sim_id, token))  # sent once
            st.session_state["import_notice"] = (sim_id, tn, text)
            invalidate_loaded()  # reload the workflow list so the new incident appears
        st.session_state.pop(f"imp_token_{sim_id}", None)
        st.rerun()


def investigate_panel(workflow_id: str | None, *, sim_id: str | None = None) -> None:
    """Start and follow a read-only investigation through the existing API. A simulator incident is first imported
    (idempotent, operations store only). Nothing here executes, approves or retries a recovery."""
    scope = f"sim:{sim_id}" if sim_id else f"wf:{workflow_id}"
    tracked = st.session_state.setdefault("investigations", {})
    st.subheader("Investigate")
    ready = client.ready()
    body = ready.data if ready.ok else (ready.problem.body if ready.problem else None)
    mode = ((body or {}).get("checks") or {}).get("investigation_mode")
    if mode == "live":
        st.caption("Mode: live. The incident text and collected diagnostic evidence are sent to the configured OpenAI model "
                   "(a few calls, token cost applies). Both are treated as untrusted data.")
    elif mode == "offline":
        st.caption("Mode: offline. Deterministic analysis of read-only diagnostic evidence; no model is called and nothing leaves the API.")
    else:
        st.caption("Investigation mode could not be read from the API.")
    st.caption("Collects read-only diagnostics, then reports observed facts separately from unconfirmed hypotheses. It executes "
               "no recovery; any recovery still needs Human Review and an approver's decision.")
    env_token = config.investigate_token()
    with st.form(f"investigate_form_{scope}", clear_on_submit=True):
        typed = "" if env_token else st.text_input("Your token (needs investigation:run)", type="password", key=f"inv_token_{scope}")
        go_clicked = st.form_submit_button("Investigate", key=f"inv_go_{scope}")
    if go_clicked:
        token = env_token or typed
        outcome = None
        if not token:
            outcome = ("error", "A token that may start investigations is required. Nothing was started.")
        else:
            target = workflow_id
            if sim_id:
                imp = client.import_simulator_incident(sim_id, token)  # idempotent; operations store only
                if imp.ok:
                    target = (imp.data.get("incident") or {}).get("incident_id")
                    st.session_state.pop("loaded", None)
                else:
                    tn, text, _ = d.investigate_start_outcome(imp)
                    outcome = (tn, "Importing the simulator incident failed: " + text)
                    target = None
            if target:
                res = client.start_investigation(target, token)  # sent once, never retried
                tn, text, iid = d.investigate_start_outcome(res)
                outcome = (tn, text)
                if iid:
                    tracked[scope] = iid
                    with st.spinner("Waiting for the investigation to finish (up to 10 seconds)..."):
                        for _ in range(10):  # a bounded wait; otherwise 'Check status' refreshes it
                            chk = client.investigation(iid)
                            if not chk.ok or chk.data.get("status") not in d.ACTIVE_STATES:
                                break
                            time.sleep(1.0)
        if outcome:
            st.session_state["investigate_notice"] = (scope, *outcome)
        st.session_state.pop(f"inv_token_{scope}", None)  # the typed token is not kept in session state
        invalidate_loaded()
        st.rerun()
    notice = st.session_state.get("investigate_notice")
    iid = tracked.get(scope)
    if not iid and workflow_id:  # a returning visit: show the incident's latest recorded investigation
        past = client.investigations(workflow_id)
        if past.ok and past.data:
            iid = max(past.data, key=lambda x: x.get("created_at") or "")["investigation_id"]
            st.caption("Showing the most recent investigation recorded for this incident.")
    det = client.investigation(iid) if iid else None
    finished = det is not None and det.ok and det.data.get("status") not in d.ACTIVE_STATES
    if notice and notice[0] == scope and not (notice[1] == "info" and finished):  # "accepted, not finished" is stale once it finished
        tone(notice[1], notice[2])
    if iid:
        st.button("Check status", key=f"inv_check_{scope}")
        if not det.ok:
            show_problem(det.problem, "the investigation status")
        else:
            render_report(det.data)
            target_id = det.data.get("incident_id")
            if det.data.get("status") == "completed" and target_id:
                st.subheader("Recovery proposal")
                note = st.session_state.get("recovery_notice")
                if note and note[0] == iid:
                    tone(note[1], note[2])
                st.caption("Optional. Asks the API to derive a recovery proposal from this investigation's evidence. It only creates a "
                           "pending request for Human Review; nothing is executed until an authorised approver decides.")
                with st.form(f"recovery_form_{iid}", clear_on_submit=True):
                    rtyped = "" if env_token else st.text_input("Your token (needs recovery:request)", type="password", key=f"rec_token_{iid}")
                    rgo = st.form_submit_button("Request recovery proposal", key=f"rec_go_{iid}")
                if rgo:
                    rtoken = env_token or rtyped
                    if not rtoken:
                        outcome2 = ("error", "A token that may request recoveries is required. Nothing was requested.")
                    else:
                        outcome2 = d.recovery_request_outcome(client.request_recovery(target_id, rtoken))  # sent once
                    st.session_state["recovery_notice"] = (iid, *outcome2)
                    st.session_state.pop(f"rec_token_{iid}", None)
                    invalidate_loaded()
                    st.rerun()


# --------------------------------------------------------------------------------------------- overview
if page == "Overview":
    st.header("Overview")
    freshness(data)
    metrics = d.overview_metrics(data)
    cols = st.columns(len(metrics))
    for col, m in zip(cols, metrics):
        col.metric(m.label, "n/a" if m.value is None else m.value, help=m.note)
    st.subheader("Sources kept separate")
    st.caption("Real workflow incidents, SQL Server simulator incidents and synthetic demo incidents are never added together.")
    srcs = d.summary_metrics(client.dashboard_summary())
    for col, m in zip(st.columns(len(srcs)), srcs):
        col.metric(m.label, "n/a" if m.value is None else m.value, help=m.note)
    if not data.available:
        st.warning("Metrics are unavailable (shown as n/a, not zero) because the incident list could not be loaded.")
    elif not data.items:
        st.info("No incidents have been recorded yet.")
    else:
        left, right = st.columns(2)
        sev = charts.severity_chart(d.count_by(data.items, "severity", d.SEVERITIES))
        wf = charts.workflow_chart(d.count_by(data.items, "workflow_status", d.WORKFLOW_ORDER))
        show_chart(left, sev, "No severity data.")
        show_chart(right, wf, "No workflow data.")
        tl = charts.timeline_chart(d.created_per_day(data.items))
        show_chart(st, tl, "No creation dates to chart.")
        st.caption("Charts use only the incidents loaded above. No history is inferred or filled in.")

# --------------------------------------------------------------------------------------------- incidents
elif page == "Incidents":
    st.header("Incident explorer")
    source = st.radio("Source", [SRC_WF, SRC_SIM, SRC_DEMO], horizontal=True, key="inc_source",
                      help="Workflow = persisted investigations/approvals. Simulator = SQL Server dataops_sim. Demo = isolated synthetic data.")
    if source == SRC_SIM:
        st.caption("Source of truth: the SQL Server simulator database (read-only here). These are confirmed simulator records, "
                   "not investigation results. Importing one into the workflow is an explicit, idempotent API action.")
        a, b, c, e = st.columns([2, 1, 1, 1])
        sq = a.text_input("Search (id, title, category, pipeline, run)", key="sim_q")
        ssev = b.selectbox("Severity", [ANY, *d.SEVERITIES], key="sim_sev")
        sst = c.selectbox("Status", [ANY, "open", "investigating", "resolved"], key="sim_status")
        simp = e.selectbox("Import state", [ANY, "not imported", "imported"], key="sim_imp")
        filters = dict(q=sq or None, severity=None if ssev == ANY else ssev, status=None if sst == ANY else sst,
                       imported=None if simp == ANY else simp == "imported")
        everything = client.simulator_incidents(limit=1)
        imported_n = client.simulator_incidents(limit=1, imported=True)
        if everything.ok and imported_n.ok:
            m1, m2, m3 = st.columns(3)
            m1.metric("Simulator incidents (all)", everything.data["total"])
            m2.metric("Imported into workflow", imported_n.data["total"])
            m3.metric("Not imported", everything.data["total"] - imported_n.data["total"])
        sim_page_size = 25
        spg = int(st.number_input("Page (25 per page)", min_value=1, value=1, step=1, key="sim_page"))
        res = client.simulator_incidents(limit=sim_page_size, offset=(spg - 1) * sim_page_size, **filters)
        if not res.ok:
            show_problem(res.problem, "simulator incidents")
        elif res.data["total"] == 0:
            st.info("No simulator incidents match the filters." if any(v is not None for v in filters.values())
                    else "The simulator has recorded no incidents. Run `python -m scripts.db_seed_demo_incidents` to add the "
                         "synthetic demonstration set.")
        elif not res.data["items"]:
            st.info(f"There are {res.data['total']} matching incidents, but page {spg} is past the end. Go back to page 1.")
        else:
            items = res.data["items"]
            pager(res.data["total"], spg, max(1, -(-res.data["total"] // sim_page_size)), "simulator incidents")
            st.caption("Imported = already present in the workflow store.")
            st.dataframe(pd.DataFrame([d.sim_row(i) for i in items]), hide_index=True, width="stretch")
            chosen = st.selectbox("Show details for", [i["incident_id"] for i in items], key="sim_pick",
                                  format_func=lambda x: x + " - " + next((i["title"][:70] for i in items if i["incident_id"] == x), ""))
            det = client.simulator_incident(chosen)
            if not det.ok:
                show_problem(det.problem, "the simulator incident")
            else:
                inc = det.data
                st.subheader(inc["title"])
                st.markdown(f"{badge('severity', inc['severity'])} {badge('status', inc['status'])} :gray-badge[simulator data]")
                st.write(inc["description"])
                st.caption(f"Created {inc['created_at']} - pipeline {inc.get('pipeline_id') or 'none'} - run {inc.get('run_id') or 'none'}")
                if inc.get("run"):
                    r = inc["run"]
                    st.write(f"**Run {r['run_id']}:** {r['status']} - {r.get('records_processed')} of {r.get('records_expected')} records"
                             + (f" - error {r['error_code']}: {r['error_message']}" if r.get("error_code") else ""))
                if inc.get("validations"):
                    st.dataframe(pd.DataFrame(inc["validations"]), hide_index=True, width="stretch")
                st.info(inc["evidence_note"])
                st.divider()
                import_panel(chosen, inc["workflow_incident_id"] if inc["imported"] else None)
                st.divider()
                investigate_panel(inc["workflow_incident_id"] if inc["imported"] else None, sim_id=chosen)
    elif source == SRC_DEMO:
        demo_banner()
        sm = client.demo_summary()
        if not sm.ok:
            show_problem(sm.problem, "the demo summary")
        else:
            x = sm.data
            for col, (label, val) in zip(st.columns(5), [("Demo incidents", x["total"]), *[(k.title(), x["by_severity"].get(k, 0)) for k in ("critical", "high", "medium", "low")]]):
                col.metric(label, val)
            a, b, c, e = st.columns(4)
            dcat = a.selectbox("Category", [ANY, *x["by_category"]], key="demo_cat")
            dsev = b.selectbox("Severity", [ANY, *d.SEVERITIES], key="demo_sev")
            dst = c.selectbox("Status", [ANY, "open", "investigating", "awaiting_review", "resolved", "closed"], key="demo_status")
            dq = e.text_input("Search", key="demo_q")
            pg = st.number_input("Page (25 per page)", min_value=1, value=1, step=1, key="demo_page")
            res = client.demo_incidents(limit=25, offset=(int(pg) - 1) * 25, q=dq or None, category=None if dcat == ANY else dcat,
                                        severity=None if dsev == ANY else dsev, status=None if dst == ANY else dst)
            if not res.ok:
                show_problem(res.problem, "demo incidents")
            elif not res.data["items"]:
                st.info("No demo incidents match the filters.")
            else:
                pager(res.data["total"], int(pg), max(1, -(-res.data["total"] // 25)), "demo incidents")
                st.dataframe(pd.DataFrame([d.demo_row(i) for i in res.data["items"]]), hide_index=True, width="stretch")
                chosen = st.selectbox("Show details for", [i["incident_id"] for i in res.data["items"]], key="demo_pick")
                det = client.demo_incident(chosen)
                if not det.ok:
                    show_problem(det.problem, "the demo incident")
                else:
                    inc = det.data
                    st.subheader(inc["title"])
                    st.markdown(f"{badge('severity', inc['severity'])} {badge('status', inc['status'])} :violet-badge[DEMO - synthetic]")
                    st.write(inc["description"])
                    st.caption(f"{inc['category']} - pipeline {inc['affected_pipeline']} - detected {inc['detected_at']} - synthetic run {inc['run_ref']}")
                    a, b = st.columns(2)
                    a.metric("Expected value", inc["expected_value"])
                    b.metric("Observed value", inc["observed_value"])
                    st.write("**Diagnostic evidence (synthetic):**")
                    st.dataframe(pd.DataFrame(inc["evidence"]), hide_index=True, width="stretch")
                    st.write(f"**Probable cause - a hypothesis, unconfirmed:** {inc['probable_cause']}")
                    st.write(f"**Recommended next step:** {inc['recommended_next_step']}")
                    st.write(f"**Human review flag:** {'yes' if inc['human_review_required'] else 'no'}")
                    st.caption(inc["note"])
                    st.caption("Synthetic demo incidents have no workflow record, so they cannot be investigated. Import a SQL Server "
                               "simulator incident, or open a workflow incident, to run a real investigation.")
    else:
        freshness(data)
        rows = [d.incident_row(i) for i in data.items]
        c1, c2, c3 = st.columns([2, 1, 1])
        text = c1.text_input("Search (id, title, type, ticket)", key="q")
        sev = c2.multiselect("Severity", d.SEVERITIES, key="sev")
        wfs = c3.multiselect("Workflow status", d.WORKFLOW_ORDER, key="wf")
        s1, s2, s3, s4 = st.columns(4)
        sort_by = s1.selectbox("Sort by", list(d.SORT_KEYS), format_func=d.SORT_KEYS.get, key="sort")
        desc = s2.checkbox("Descending / most severe first", value=True, key="desc")
        size = s3.selectbox("Page size", d.PAGE_SIZES, index=1, key="size")
        page_no = s4.number_input("Page", min_value=1, value=1, step=1, key="pageno")
        view = d.apply_view(rows, text=text, severities=tuple(sev), workflows=tuple(wfs), sort_by=sort_by, descending=desc,
                            page=int(page_no), page_size=int(size))
        if not rows:
            st.info("No incidents to show." if data.available else "Incidents are unavailable.")
        elif not view.rows:
            st.info("No incidents match the current filters.")
        else:
            pager(view.total_matching, view.page, view.page_count, "incidents")
            st.dataframe(pd.DataFrame(view.rows), hide_index=True, width="stretch")
            chosen = pick_incident("Show details for", "inc_pick", [r["incident_id"] for r in view.rows])
            if chosen:
                inc = by_id[chosen]
                st.subheader(inc["title"])
                a, b, c = st.columns(3)
                a.write(f"**Severity:** {inc['severity']}")
                b.write(f"**Status:** {inc['status']}")
                c.write(f"**Workflow:** {inc['workflow_status']}")
                st.markdown(f"{badge('severity', inc['severity'])} {badge('status', inc['workflow_status'])}")
                st.write(inc["description"])
                st.link_button("Events for this incident in VerbaLedger", f"{config.ledger_explorer_url()}/?page=Events&incident_id={inc['incident_id']}")
                st.caption(f"Created {inc['created_at']} by {inc['created_by']}; updated {inc['updated_at']}; external ref: {inc.get('external_ref') or 'none'}")
                tv = d.ticket_view(inc.get("ticket"))
                st.write("**Ticket:** " + (f"{inc['ticket'].get('provider')} {inc['ticket'].get('status')}" if tv["present"] else "none"))
                if tv["caution"]:
                    st.info(tv["caution"])
                st.divider()
                investigate_panel(chosen)

# --------------------------------------------------------------------------------------------- investigations
elif page == "Investigations":
    st.header("Investigation explorer")
    ids = [i["incident_id"] for i in data.items]
    chosen = pick_incident("Incident", "inv_inc", ids)
    if chosen is None:
        st.info("No incidents to investigate yet." if data.available else "Incidents are unavailable.")
    else:
        investigate_panel(chosen)
        st.divider()
        res = client.investigations(chosen)
        if not res.ok:
            show_problem(res.problem, "investigations")
        elif not res.data:
            st.info("This incident has no investigations.")
        else:
            st.dataframe(pd.DataFrame([{k: v for k, v in inv.items() if k not in ("evidence_refs", "error")} for inv in res.data]),
                         hide_index=True, width="stretch")
            inv_id = st.selectbox("Open investigation", [i["investigation_id"] for i in res.data], key="inv_pick")
            det = client.investigation(inv_id)
            if not det.ok:
                show_problem(det.problem, "the investigation")
            else:
                render_report(det.data)

# --------------------------------------------------------------------------------------------- human review
elif page == "Human Review":
    st.header("Human Review")
    st.caption("Only items that are genuinely waiting for a human decision in the persisted approval workflow. Synthetic demo "
               "flags and simulator records never appear here. Approving does not bypass governance: execution still happens only "
               "inside the VSL-gated recovery workflow, and this page executes nothing itself.")
    if "decision_result" in st.session_state:  # shown here so it stays visible after the item leaves the pending list
        tone(*st.session_state["decision_result"])
    summ = client.reviews_summary()
    guide = d.recovery_guidance(summ.problem)
    if guide:  # recovery/approvals genuinely switched off: say exactly why and stop; draw no queue and no zero counts
        show_recovery_guidance(guide)
        st.stop()
    if not summ.ok:
        show_problem(summ.problem, "the review summary")
    else:
        sv = summ.data
        cols = st.columns(5)
        cols[0].metric("Pending human review", sv["pending"])
        for col, sev_name in zip(cols[1:], ("critical", "high", "medium", "low")):
            col.metric(f"Pending - {sev_name}", sv["pending_by_severity"].get(sev_name, 0))
        if sv.get("oldest_pending"):
            st.caption(f"Oldest pending request created {sv['oldest_pending']}.")
    a, b, c = st.columns([1, 2, 1])
    state = a.radio("Show", ["pending", "decided", "expired", "all"], key="rev_state")
    rq = b.text_input("Search (incident, action, target, requester)", key="rev_q")
    rsev = c.selectbox("Incident severity", [ANY, *d.SEVERITIES], key="rev_sev")
    res = client.reviews(state=state, q=rq or None, severity=None if rsev == ANY else rsev, limit=100)
    if not res.ok:
        show_problem(res.problem, "the review queue")
    elif not res.data["items"]:
        st.info("Nothing is awaiting a human decision." if state == "pending" and not rq and rsev == ANY
                else "No review items match the current filters.")
    else:
        items = res.data["items"]
        st.caption(f"{res.data['total']} item(s) ({state}).")
        st.dataframe(pd.DataFrame([d.review_row(i) for i in items]), hide_index=True, width="stretch")
        ids = [i["review_id"] for i in items]
        focus = st.session_state.get("focus_review")
        focus = focus if focus in ids else next((i["review_id"] for i in items if i["request_id"] == focus), None)
        if focus and "rev_pick" not in st.session_state:
            st.session_state["rev_pick"] = focus
        chosen = st.selectbox("Open review item", ids, key="rev_pick",
                              format_func=lambda x: next((f"{i['incident_id']} - {i['action_type']} ({i['review_state']})" for i in items if i["review_id"] == x), x))
        det = client.review(chosen)  # fresh every time: approval state is never cached
        if not det.ok:
            show_problem(det.problem, "the review item")
        else:
            it = det.data
            st.subheader(f"{it['incident_id']} - {it['incident_title']}")
            st.markdown(f"{badge('severity', it['incident_severity'])} {badge('status', it['review_state'])} :gray-badge[risk: {it['risk']}]")
            t1, t2, t3, t4 = st.tabs(["Confirmed evidence", "Agent hypothesis", "Recommendation", "Human decision"])
            with t1:
                ev = it["confirmed_evidence"]
                st.write(f"Report status: **{ev['report_status']}** - evidence references: {', '.join(ev['evidence_refs']) or 'none'}")
                if ev["findings"]:
                    st.dataframe(pd.DataFrame(ev["findings"]), hide_index=True, width="stretch")
            with t2:
                hy = it["agent_hypothesis"]
                st.write(f"Status: **{hy['status'] or 'none recorded'}** - confidence: {hy['confidence'] or 'n/a'}")
                st.write(hy["statement"] or "No root-cause statement was recorded.")
                st.caption(hy["note"])
            with t3:
                rc = it["recommendation"]
                st.write(f"**Action:** {rc['action_type']} on **{it['target']}** - parameters {it['parameters']}")
                st.write(f"**Reason:** {rc['reason']}")
                st.write(f"**Expected effect:** {rc['expected_effect']}")
                st.caption(f"Governance state when requested: {it['governance_state']}. Requested by {it['requested_by']} at {it['created_at']}; "
                           f"expires {it['expires_at']}.")
            with t4:
                hd = it["human_decision"]
                if hd["decided_by"]:
                    st.write(f"**{hd['state'].title()}** by {hd['decided_by']}: {hd['reason'] or '(no reason recorded)'}")
                else:
                    st.write(f"No decision recorded. State: **{hd['state']}**.")
            with st.expander("Governance trail (event names; the full trace is in the VerbaLedger Explorer)"):
                if it["governance_events"]:
                    st.dataframe(pd.DataFrame(it["governance_events"]), hide_index=True, width="stretch")
                else:
                    st.write("No governance events are readable for this item.")
                st.link_button("Trace this request in VerbaLedger", f"{config.ledger_explorer_url()}/?page=Causal+trace&request_id={it['request_id']}")
            st.caption("Clarification requests are not supported by the approval workflow, so none are offered.")
            if d.review_actionable(it):
                decision_panel(it["request_id"])

# --------------------------------------------------------------------------------------------- pipelines & runs
elif page == "Pipelines & runs":
    st.header("Pipelines and runs")
    st.caption("Source: the SQL Server simulator (synthetic simulation data, read-only).")
    pl = client.simulator_pipelines()
    if not pl.ok:
        show_problem(pl.problem, "pipelines")
    elif not pl.data["items"]:
        st.info("The simulator has no pipeline definitions.")
    else:
        st.dataframe(pd.DataFrame(pl.data["items"]), hide_index=True, width="stretch")
        a, b = st.columns(2)
        pid = a.selectbox("Pipeline", [ANY, *[p["pipeline_id"] for p in pl.data["items"]]], key="run_pipe")
        rst = b.selectbox("Run status", [ANY, "success", "failed", "partial", "running"], key="run_status")
        runs = client.simulator_runs(pipeline_id=None if pid == ANY else pid, status=None if rst == ANY else rst, limit=50)
        if not runs.ok:
            show_problem(runs.problem, "runs")
        elif not runs.data["items"]:
            st.info("No runs match the filters.")
        else:
            st.dataframe(pd.DataFrame(runs.data["items"]), hide_index=True, width="stretch")

# --------------------------------------------------------------------------------------------- recovery & approvals
elif page == "Recovery & approvals":
    st.header("Recovery and approval monitoring")
    st.caption("Recovery runs only inside the governed Phase 7 workflow after a human approval. This page never executes or retries anything.")
    candidates = [i["incident_id"] for i in data.items if i.get("workflow_status") in d.RECOVERY_WORKFLOWS]
    if not data.available:
        st.info("Incidents are unavailable.")
    elif not candidates:
        st.info("No incident currently has a recovery request.")
    else:
        pending = sum(1 for i in data.items if i.get("workflow_status") == "awaiting_approval")
        uncertain = sum(1 for i in data.items if i.get("workflow_status") == "recovery_uncertain")
        a, b = st.columns(2)
        a.metric("Awaiting approval", pending)
        b.metric("Outcome uncertain", uncertain)
        chosen = pick_incident("Incident with a recovery", "rec_inc", candidates)
        recs = client.recoveries(chosen)
        if not recs.ok:
            show_problem(recs.problem, "recoveries")
        elif not recs.data:
            st.info("No recovery records for this incident.")
        else:
            st.dataframe(pd.DataFrame([{k: r.get(k) for k in ("recovery_id", "status", "phase", "execution_status", "verified_outcome",
                                                              "action_type", "requested_by", "created_at")} for r in recs.data]),
                         hide_index=True, width="stretch")
            rec_id = st.selectbox("Open recovery", [r["recovery_id"] for r in recs.data], key="rec_pick")
            res = client.recovery(rec_id)  # fresh every time: approval state is never cached
            if not res.ok:
                show_problem(res.problem, "the recovery")
            else:
                rec = res.data
                summary = d.recovery_summary(rec)
                tone(summary["tone"], summary["headline"])
                for note in summary["notes"]:
                    st.caption(note)
                st.write(f"**Execution status:** {summary['execution_status']} - **Verified outcome:** {summary['verified_outcome']}")
                st.write(f"**Action:** {rec['action_type']} - **Parameters:** {rec.get('parameters')}")
                ap = rec.get("approval")
                if ap:
                    st.subheader("Approval request")
                    st.write(f"**Status:** {ap['status']} - **Risk:** {ap['risk']} - **Expires:** {ap['expires_at']}")
                    st.write(f"**Target:** {ap['target']} - **Expected effect:** {ap['expected_effect']}")
                    st.write(f"**Reason:** {ap['reason']}")
                    if ap.get("decided_by"):
                        st.write(f"**Decided by:** {ap['decided_by']} - {ap.get('decision_reason') or ''}")
                if rec.get("governance_events"):
                    with st.expander("Governance trail (event names; use the VerbaLedger Explorer for the full trace)"):
                        st.dataframe(pd.DataFrame(rec["governance_events"]), hide_index=True, width="stretch")
                if d.approval_actionable(rec):
                    decision_panel(rec["approval_request_id"])
                if "decision_result" in st.session_state:
                    tone(*st.session_state["decision_result"])

# --------------------------------------------------------------------------------------------- tickets
elif page == "Tickets":
    st.header("Ticketing")
    rows = d.ticket_rows(data.items)
    if not data.available:
        st.info("Incidents are unavailable.")
    elif not rows:
        st.info("No incident has a ticket yet.")
    else:
        st.caption(f"{len(rows)} of {len(data.items)} loaded incidents have a ticket.")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        chosen = pick_incident("Ticket for incident", "tk_inc", [r["incident_id"] for r in rows])
        res = client.ticket(chosen)
        if not res.ok:
            show_problem(res.problem, "the ticket")
        else:
            t = res.data
            st.write(f"**{t['provider']}** {t['repository']} #{t.get('number')} - status **{t['status']}** - last sync: {t.get('last_sync_result') or 'never'}")
            caution = d.ticket_view(t)["caution"]
            if caution:
                st.info(caution)
            if t.get("recent_events"):
                with st.expander("Recent ticket events"):
                    st.dataframe(pd.DataFrame(t["recent_events"]), hide_index=True, width="stretch")

# --------------------------------------------------------------------------------------------- status
else:
    st.header("Backend connectivity")
    rows, warnings = d.status_rows(client.health(), client.ready())
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    for w in warnings:
        st.warning(w)
    st.caption(f"API base URL: {client.base_url} - token: {'configured' if client.has_token else 'not configured'} (value never shown)")
