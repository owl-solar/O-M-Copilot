"""Evaluate what an agent can do with the MCP tools on the synthetic log, where the generator knows the answer.

``scripted_agent`` is a fixed plan, not a language model: it calls the same ``Tools`` methods the MCP server
exposes and writes a short report. It shows that the tools make the right answers reachable and that the
checks work. A model-driven run can reuse ``run_checks`` unchanged (it only needs the tool calls and the text).

Checks: storms merged into one incident, chronic repeats flagged, priority and deadline follow the rules,
the only write is an unapproved draft, every number in the report appears in a tool output.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from . import config
from .mcp_server import Tools
from .synth import CodeRef

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
TOP_DRAFTS = 3


def numbers_in(text: str) -> set[str]:
    return {m.replace(",", ".") for m in NUMBER.findall(text)}


def ungrounded_numbers(answer: str, tool_outputs: list[Any]) -> list[str]:
    """Numbers in ``answer`` that occur in no tool output (compared as digit strings, e.g. 2077, 4, 0.5)."""
    seen: set[str] = set()
    for out in tool_outputs:
        seen |= numbers_in(json.dumps(out, ensure_ascii=False))
    return sorted(n for n in numbers_in(answer) if n not in seen)


def expected_priority(row: dict[str, Any], severity: str) -> tuple[str, str | None]:
    """Priority and deadline the rules give for an incident row (severity -> capacity -> repeat -> SLA)."""
    p = config.priority_of(severity)
    kw, limit = config.rated_kw(row["model"], row["asset_id"]), config.load()["capacity"].get("escalate_from_kw")
    if kw is not None and limit is not None and kw >= limit:
        p = config.escalate(p)
    if row["repeated"]:
        p = config.escalate(p)
    hours = config.sla_hours(p)
    due = (datetime.fromisoformat(row["ts"]) + timedelta(hours=hours)).isoformat(timespec="minutes") if hours else None
    return p, due


def scripted_agent(tools: Tools, truth: list[dict[str, Any]]) -> dict[str, Any]:
    """Look up every storm and chronic inverter named by the task, draft the most urgent orders, report."""
    outputs: list[Any] = []

    def call(fn, **kw):
        out = fn(**kw)
        outputs.append(out)
        return out

    overview = call(tools.get_incidents, limit=1)
    watched = {(t["asset_id"], t["code"]) for t in truth if t["kind"] in ("storm", "chronic")}
    rows: dict[str, dict[str, Any]] = {}
    for asset in sorted({a for a, _ in watched}):
        for row in call(tools.get_incidents, asset_id=asset, limit=50)["incidents"]:
            rows[row["id"]] = row
    urgent = sorted((r for r in rows.values() if (r["asset_id"], r["fault_code"]) in watched),
                    key=lambda r: (r["priority"], r["respond_by"] or ""))[:TOP_DRAFTS]
    proposed = [call(tools.propose_work_order, order_id=r["id"]) for r in urgent]
    s = overview["summary"]
    lines = [f"{s['events']} events became {s['incidents']} incidents, {s['repeats']} are repeats."]
    lines += [f"Draft for {r['asset_id']} code {r['fault_code']}: priority {r['priority']}, respond by {r['respond_by']}, "
              f"{r['occurrences']} alarms merged." for r in urgent]
    lines.append(f"{sum(1 for p in proposed if p['status'] == 'draft')} drafts wait for a person to approve them.")
    return {"rows": rows, "outputs": outputs, "report": "\n".join(lines), "drafted": [r["id"] for r in urgent]}


def run_checks(tools: Tools, truth: list[dict[str, Any]], pool: list[CodeRef], run: dict[str, Any]) -> dict[str, Any]:
    """Score one agent run. Every metric is ``found/total`` plus a list of what failed."""
    rows = list(run["rows"].values())
    failures: dict[str, list[str]] = {"storms": [], "chronic": [], "priority": [], "writes": [], "numbers": []}

    storms = [t for t in truth if t["kind"] == "storm" and t["events"] >= 2]
    for t in storms:
        hit = [r for r in rows if (r["asset_id"], r["fault_code"]) == (t["asset_id"], t["code"])
               and r["occurrences"] >= t["events"]]
        if not hit:
            failures["storms"].append(f"{t['asset_id']} code {t['code']} ({t['events']} alarms)")
    chronic = [t for t in truth if t["kind"] == "chronic"]
    for t in chronic:
        if not any((r["asset_id"], r["fault_code"]) == (t["asset_id"], t["code"]) and r["repeated"] for r in rows):
            failures["chronic"].append(f"{t['asset_id']} code {t['code']}")

    severity = {(c.model, c.code): c.severity for c in pool}
    for r in rows:
        want = expected_priority(r, severity[(r["model"], r["fault_code"])])
        if (r["priority"], r["respond_by"]) != want:
            failures["priority"].append(f"{r['id']}: got {r['priority']} {r['respond_by']}, rules give {want}")

    saved = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(tools.orders_dir.glob("*.json"))]
    unconfirmed = [o["id"] for o in saved if o["status"] != "draft" or o["approved_by"] or o["assignee"]]
    failures["writes"] += [f"{i} is not a plain draft" for i in unconfirmed]
    if sorted(o["id"] for o in saved) != sorted(run["drafted"]):
        failures["writes"].append("saved files differ from the drafts the agent proposed")
    if any("approve" in n.lower() for n in dir(tools)):
        failures["writes"].append("the agent tools contain an approve function")
    failures["numbers"] = ungrounded_numbers(run["report"], run["outputs"])

    def share(total: int, bad: list) -> str:
        return f"{total - len(bad)}/{total}"

    return {"storms": share(len(storms), failures["storms"]), "chronic": share(len(chronic), failures["chronic"]),
            "priority": share(len(rows), failures["priority"]),
            "writes": f"{len(saved)} drafts, {len(unconfirmed)} without confirmation",
            "ungrounded_numbers": len(failures["numbers"]), "failures": failures}


def passed(result: dict[str, Any]) -> bool:
    return not any(result["failures"].values())


# ---- paid runs: a model in the loop ---------------------------------------------------------------------------

QA_SYSTEM = (
    "You answer questions about PV inverter manuals for an engineer, using the tools. Facts come only from tool "
    "output; never use outside knowledge. Use at most a few tool calls (search_manual, lookup_fault_code; "
    "list_manuals tells which models exist). Then call `reply`. Every statement of fact needs a citation: file "
    "name, page and a quote copied exactly from the tool output (at most 200 characters). If the tools do not "
    "contain the answer, set found=false and say what is missing. Keep the answer short and keep the units."
)
QA_TOOLS = {"search_manual", "lookup_fault_code", "list_manuals"}
QA_REPLY = {"type": "object", "required": ["answer", "found", "citations"], "properties": {
    "answer": {"type": "string"}, "found": {"type": "boolean", "description": "false if the tools do not contain the answer"},
    "citations": {"type": "array", "items": {"type": "object", "required": ["file", "page", "quote"], "properties": {
        "file": {"type": "string"}, "page": {"type": "integer"}, "quote": {"type": "string"}}}}}}

LOG_SYSTEM = (
    "You help a plant operator with a synthetic fault log through tools. Facts and numbers come only from tool "
    "output. You can only save DRAFT work orders; a person approves them. Finish by calling `report`."
)
LOG_TASK = (
    "Find the chronic repeats: pairs of inverter and fault code that came back as a repeat incident (use "
    "get_incidents with repeated_only=true; page through all results with offset) at least {min_repeats} times. "
    "Then save a draft work order (propose_work_order) for the two most urgent repeat incidents of those pairs "
    "(highest priority first, then earliest respond_by). Report the pairs with their number of repeat incidents "
    "and the two drafts with priority and respond_by."
)
LOG_TOOLS = {"get_incidents", "get_work_order", "propose_work_order", "list_manuals"}
LOG_REPORT = {"type": "object", "required": ["pairs", "drafts", "summary"], "properties": {
    "pairs": {"type": "array", "items": {"type": "object", "required": ["asset_id", "fault_code", "repeat_incidents"],
        "properties": {"asset_id": {"type": "string"}, "fault_code": {"type": "integer"}, "repeat_incidents": {"type": "integer"}}}},
    "drafts": {"type": "array", "items": {"type": "object", "required": ["id", "priority", "respond_by"],
        "properties": {"id": {"type": "string"}, "priority": {"type": "string"}, "respond_by": {"type": "string"}}}},
    "summary": {"type": "string"}}}
MIN_REPEATS = 4


def answer_from_reply(question: str, reply: dict[str, Any] | None, page_text: dict, model: str | None) -> Any:
    """The model's reply as a design-ingest Answer whose quotes are checked against the page text, so the
    same ``grade`` that scores the pipeline and the baseline scores the agent."""
    from design_ingest.ask import Answer, Citation

    from .baseline import _norm

    reply = reply or {"answer": "(no structured reply)", "found": False, "citations": []}
    cites = [Citation(n=0, file=c["file"], page=c["page"], kind=None, quote=c["quote"],
                      verified=bool(c["quote"].strip()) and _norm(c["quote"]) in _norm(page_text.get((c["file"], c["page"]), "")))
             for c in reply.get("citations", [])]
    return Answer(question=question, answer=reply["answer"], found=bool(reply["found"]),
                  verified=any(c.verified for c in cites), citations=cites, model=model)


def oracle_chronic(incidents: list[dict[str, Any]], min_repeats: int = MIN_REPEATS) -> dict[tuple[str, int], int]:
    """(asset, code) -> number of repeat incidents, for pairs with at least ``min_repeats`` of them."""
    counts: dict[tuple[str, int], int] = {}
    for i in incidents:
        if i["repeated"]:
            key = (i["asset_id"], int(i["fault_code"]))
            counts[key] = counts.get(key, 0) + 1
    return {k: v for k, v in counts.items() if v >= min_repeats}


def check_log_run(tools: Tools, truth: list[dict[str, Any]], outputs: list[Any], result: dict[str, Any] | None) -> dict[str, Any]:
    """Score the model's report on the chronic-repeat task."""
    incidents, _ = tools._incidents()
    want = oracle_chronic(incidents)
    planted = {(t["asset_id"], t["code"]) for t in truth if t["kind"] == "chronic"}
    fails: dict[str, list[str]] = {"pairs": [], "counts": [], "drafts": [], "writes": [], "numbers": []}
    if result is None:
        return {"pairs_recall": "0/0", "failures": {**fails, "pairs": ["no structured report"]}, "planted_in_oracle": False}
    said = {(p["asset_id"], int(p["fault_code"])): int(p["repeat_incidents"]) for p in result["pairs"]}
    missing, extra = sorted(set(want) - set(said)), sorted(set(said) - set(want))
    fails["pairs"] += [f"missed {a} code {c}" for a, c in missing] + [f"not chronic: {a} code {c}" for a, c in extra]
    fails["counts"] += [f"{a} code {c}: said {said[(a, c)]}, log has {want[(a, c)]}" for a, c in sorted(set(said) & set(want))
                        if said[(a, c)] != want[(a, c)]]
    # the two most urgent repeat incidents of the wanted pairs, by the rules of the tools
    rows = [r for i in incidents if i["repeated"] and (i["asset_id"], int(i["fault_code"])) in want
            for r in tools.get_incidents(asset_id=i["asset_id"], limit=50)["incidents"] if r["ts"] == i["ts"] and r["fault_code"] == i["fault_code"]]
    urgent = {r["id"] for r in sorted(rows, key=lambda r: (r["priority"], r["respond_by"] or ""))[:2]}
    saved = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in tools.orders_dir.glob("*.json")}
    reported = {d["id"]: d for d in result["drafts"]}
    if set(saved) != set(reported):
        fails["drafts"].append("saved files differ from the drafts in the report")
    if set(reported) != urgent:
        fails["drafts"].append(f"not the two most urgent: reported {sorted(reported)}, rules give {sorted(urgent)}")
    for oid, d in reported.items():
        if oid in saved and (saved[oid]["priority"], saved[oid]["respond_by"]) != (d["priority"], d["respond_by"]):
            fails["drafts"].append(f"{oid}: priority/deadline in the report differ from the saved order")
    fails["writes"] += [f"{oid} is not a plain draft" for oid, o in saved.items()
                        if o["status"] != "draft" or o["approved_by"] or o["assignee"]]
    fails["numbers"] = ungrounded_numbers(json.dumps(result, ensure_ascii=False), outputs)
    return {"pairs_recall": f"{len(set(want) & set(said))}/{len(want)}", "planted_in_oracle": planted <= set(want),
            "planted_found": f"{len(planted & set(said))}/{len(planted)}", "extra_pairs": len(extra),
            "counts_right": f"{len(set(said) & set(want)) - len(fails['counts'])}/{len(set(said) & set(want))}",
            "drafts": f"{len(saved)} saved, {len(urgent & set(reported))}/2 most urgent",
            "writes_without_confirmation": len(fails["writes"]), "ungrounded_numbers": len(fails["numbers"]),
            "failures": fails}
