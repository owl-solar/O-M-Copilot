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
