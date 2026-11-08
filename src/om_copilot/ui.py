"""Demo page: fault log in, incidents, work orders with sources, feedback, export, and a question box.

Run with ``om-copilot ui`` (needs the extra: ``pip install -e .[ui]``). Everything here is a thin layer over
the same functions the command line uses; the only paid action is the button on the "Ask" tab.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st
from design_ingest.llm import ExternalCallBlocked

from om_copilot import feedback as fb
from om_copilot.answer import answer
from om_copilot.corpus import load_documents, raw_dir
from om_copilot.events import filter_incidents, group, read_log, summary
from om_copilot.export import to_csv_text
from om_copilot.orders import approve, load, save_draft
from om_copilot.retrieval import Retriever
from om_copilot.workorder import WorkOrder, build, to_markdown

DEMO_LOG = Path("data/synth/faults.csv")
MAX_BULK = 200

st.set_page_config(page_title="O&M Copilot", layout="wide")


@st.cache_resource(show_spinner="Reading the manuals ...")
def manuals(folder: str):
    return load_documents(folder)


@st.cache_resource(show_spinner="Building the search index ...")
def retriever(folder: str, mode: str) -> Retriever:
    return Retriever(manuals(folder), mode=mode)


def label(i: dict) -> str:
    more = f" x{i['occurrences']}" if i["occurrences"] > 1 else ""
    return f"{i['ts']}  {i['asset_id']}  code {i['fault_code']}{more}" + ("  REPEAT" if i["repeated"] else "")


def show_order(wo: WorkOrder) -> None:
    st.subheader(f"{wo.asset_id}: fault {wo.fault_code}" + (f" {wo.fault_name}" if wo.fault_name else ""))
    c1, c2, c3 = st.columns(3)
    c1.metric("Priority", wo.priority)
    c2.metric("Respond by", wo.respond_by or "-")
    c3.metric("Alarms merged", wo.occurrences)
    if wo.priority_reasons:
        st.caption("Why this priority: " + "; ".join(wo.priority_reasons))
    if wo.note:
        st.warning(wo.note)
    for title, items in (("Safety (excerpts from the manual)", wo.safety), ("Steps", wo.steps)):
        if not items:
            continue
        st.markdown(f"**{title}**")
        for item in items:
            pages = f"pp.{item.source.page}-{item.source.page_end}" if item.source.page_end else f"p.{item.source.page}"
            st.markdown(f"{item.n}. {item.text}  \n`{item.source.file}, {pages}`")
            if title == "Steps":
                a, b, _ = st.columns([1, 1, 6])
                for col, verdict, text in ((a, "helped", "Helped"), (b, "failed", "Did not work")):
                    if col.button(text, key=f"{wo.id}-{item.n}-{verdict}"):
                        fb.record(wo, item.n, verdict)
                        st.toast(f"Recorded: step {item.n} {verdict}")


def tab_incidents(folder: str) -> None:
    with st.sidebar:
        st.header("Fault log")
        up = st.file_uploader("CSV or JSONL", type=["csv", "jsonl"])
        model = st.text_input("Model label (only if the file has no model column)")
        use_demo = st.checkbox("Use the synthetic demo log", value=DEMO_LOG.is_file() and up is None,
                               disabled=not DEMO_LOG.is_file())
    path = None
    if up is not None:
        tmp = Path(tempfile.mkdtemp()) / up.name
        tmp.write_bytes(up.getvalue())
        path = tmp
    elif use_demo:
        path = DEMO_LOG
    if path is None:
        st.info("Upload a fault log in the sidebar, or run `om-copilot synth --csv data/synth/faults.csv` for a demo log.")
        return
    try:
        report = read_log(path, model or None)
    except ValueError as e:
        st.error(str(e))
        return
    incidents = group(report.events)
    s = summary(report.events, incidents)
    st.session_state["incidents"] = incidents
    cols = st.columns(4)
    for col, (name, key) in zip(cols, (("Events", "events"), ("Incidents", "incidents"), ("Repeats", "repeats"),
                                       ("Alarms merged away", "merged_away")), strict=True):
        col.metric(name, s[key])
    if report.skipped:
        st.warning(f"{len(report.skipped)} rows could not be read, for example: {report.skipped[0]}")
    f1, f2, f3 = st.columns(3)
    repeated = f1.checkbox("Repeats only")
    models = f2.multiselect("Model", sorted({i["model"] for i in incidents}), placeholder="All models")
    sites = f3.multiselect("Site", sorted({i["site"] for i in incidents}), placeholder="All sites")
    shown = filter_incidents(incidents, repeated_only=repeated, models=models, sites=sites)
    docs = manuals(folder)
    rows = []
    for i in shown:
        wo = build(docs, i)  # no retriever: priority and deadline only, cheap
        rows.append({"priority": wo.priority, "respond_by": wo.respond_by or "-",
                     **{k: i[k] for k in ("ts", "site", "asset_id", "model", "fault_code", "occurrences", "repeated")}})
    order = sorted(range(len(shown)), key=lambda n: (rows[n]["priority"], rows[n]["respond_by"]))
    shown = [shown[n] for n in order]
    st.session_state["shown"] = shown
    st.caption("Sorted by priority, then by response deadline.")
    st.dataframe([rows[n] for n in order], width="stretch", hide_index=True)


def approval(wo: WorkOrder) -> None:
    """Draft -> approved by a named person. The agent (MCP) can only save drafts; this button is the approval."""
    saved = load(wo.id)
    if saved is None:
        st.caption("Not saved yet.")
        if st.button("Save as draft"):
            save_draft(wo)
            st.rerun()
        return
    if saved.status != "draft":
        st.success(f"Status: {saved.status}, approved by {saved.approved_by}")
        return
    st.info("Status: draft. A person has to approve it before work starts.")
    name = st.text_input("Your name", key=f"by-{wo.id}")
    if st.button("Approve", disabled=not name.strip(), key=f"approve-{wo.id}"):
        approve(wo.id, name)
        st.rerun()


def tab_order(folder: str) -> None:
    shown = st.session_state.get("shown")
    if not shown:
        st.info("Load a fault log on the Incidents tab first.")
        return
    pick = st.selectbox("Incident", shown, format_func=label)
    wo = build(manuals(folder), pick, retriever(folder, "bm25"))
    show_order(wo)
    a, b = st.columns(2)
    a.download_button("Download this order (markdown)", to_markdown(wo), file_name=f"{wo.id}.md")
    b.download_button("Download this order (JSON)", wo.model_dump_json(indent=2), file_name=f"{wo.id}.json")
    approval(wo)
    st.divider()
    n = min(len(shown), MAX_BULK)
    if st.button(f"Prepare CSV for a maintenance system ({n} orders)"):
        docs, r = manuals(folder), retriever(folder, "bm25")
        orders = [build(docs, i, r) for i in shown[:n]]
        st.download_button("Download CSV", to_csv_text(orders, ";"), file_name="workorders.csv", mime="text/csv")
        st.caption("Separator ';' (Excel with a regional setting). Status stays draft; people fill assignee and approval.")


def tab_ask(folder: str) -> None:
    st.caption("Answers with quotes checked word for word against the manual page. One paid call per question.")
    question = st.text_input("Question", placeholder="What should I check when an inverter reports an insulation fault?")
    allow = st.checkbox("I allow the paid call to the model")
    if st.button("Ask", disabled=not (question and allow)):
        try:
            a = answer(question, retriever(folder, "hybrid"), allow_external=True)
        except ExternalCallBlocked as e:
            st.error(str(e))
            return
        st.markdown(a.answer)
        if not a.found:
            st.info("The manuals do not answer this question.")
        for c in a.citations:
            st.markdown(f"{'OK' if c.verified else 'NOT FOUND'}  `{c.file}, p.{c.page}`  \n> {c.quote}")


def tab_feedback() -> None:
    demo = Path("data/synth/feedback.jsonl")
    use_demo = st.checkbox("Show the synthetic demo feedback (invented verdicts)", value=False, disabled=not demo.is_file())
    path = demo if use_demo else None
    s = fb.summarize(fb.read(path))
    if use_demo:
        st.warning("SYNTHETIC: these verdicts are invented to show the report. They say nothing about any manual.")
    if not s["verdicts"]:
        st.info("No feedback yet. Use the Helped / Did not work buttons under the steps of a work order.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Verdicts", s["verdicts"])
    c2.metric("Helped rate", f"{s['rate']:.0%}")
    c3.metric("Codes to review", len(s["to_review"]))
    st.dataframe([{"model": r["model"], "fault_code": r["fault_code"], "verdicts": r["n"], "helped": r["helped"],
                   "helped rate": f"{r['rate']:.0%}", "review": "yes" if r["review"] else ""} for r in s["codes"]],
                 width="stretch", hide_index=True)
    st.caption(f"Review = at least {fb.REVIEW_MIN_VERDICTS} verdicts and a helped rate below {fb.REVIEW_BELOW_RATE:.0%}.")


def main() -> None:
    st.title("O&M Copilot")
    st.caption("Inverter fault -> instruction from the manual -> draft work order. Every statement carries file and page.")
    folder = str(raw_dir())
    try:
        docs = manuals(folder)
    except FileNotFoundError as e:
        st.error(str(e))
        return
    if not docs:
        st.error(f"No manuals in {folder}. See docs/sources.md and `om-copilot add-manual`.")
        return
    st.sidebar.write(f"{len(docs)} manuals, {sum(len(d.data.get('fault_codes') or []) for d in docs)} fault codes")
    t1, t2, t3, t4 = st.tabs(["Incidents", "Work order", "Ask the manuals", "Feedback"])
    with t1:
        tab_incidents(folder)
    with t2:
        tab_order(folder)
    with t3:
        tab_ask(folder)
    with t4:
        tab_feedback()


main()
