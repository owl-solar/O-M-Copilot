"""MCP server (stdio): the existing functions as tools for an agent. No logic of its own.

Run with ``om-copilot mcp`` (needs the extra: ``pip install -e .[mcp]``). The tool functions are plain methods
of ``Tools`` so tests call them directly; ``mcp`` is imported only when the server is built.

Every answer that comes from a manual carries its source: file, page and sha256 of that exact file.
stdout belongs to the protocol: log to stderr only, never print.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from design_ingest.llm import ExternalCallBlocked

from . import config
from .answer import answer
from .corpus import load_documents
from .events import filter_incidents, group, read_log
from .manuals import manifest
from .orders import load as load_order
from .orders import orders_dir as orders_dir_default
from .orders import save_draft
from .retrieval import Retriever, record_matches
from .workorder import build, work_order_id

log = logging.getLogger(__name__)

DEFAULT_LOG = "data/synth/faults.jsonl"
MAX_ROWS = 50
TEXT_CHARS = 700
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

INSTRUCTIONS = (
    "O&M Copilot: inverter fault codes, manual search, incidents from the fault log and draft work orders. "
    "Facts about a manual come only from these tools and always carry file, page and sha256. "
    "Numbers and pages in your answer must be copied from tool output. Work orders are drafts: "
    "a person approves them, you cannot."
)


class Tools:
    """State (manuals, retriever, log) is loaded on first use; tests pass fakes in."""

    def __init__(self, docs: list[Any] | None = None, retriever: Any | None = None, log_path: str | None = None,
                 orders_dir: str | None = None, mode: str | None = None):
        self._docs = docs
        self._retriever = retriever
        self.log_path = log_path or os.environ.get("OM_COPILOT_LOG", DEFAULT_LOG)
        self.orders_dir = Path(orders_dir) if orders_dir else orders_dir_default()
        self.mode = mode or os.environ.get("OM_COPILOT_MCP_MODE", "hybrid")

    @property
    def docs(self) -> list[Any]:
        if self._docs is None:
            self._docs = load_documents()
        return self._docs

    @property
    def retriever(self) -> Any:
        if self._retriever is None:
            self._retriever = Retriever(self.docs, mode=self.mode)
        return self._retriever

    def source(self, doc: Any, page: int | None, page_end: int | None = None) -> dict[str, Any]:
        return {"file": doc.filename, "page": page, "page_end": page_end if page_end != page else None,
                "sha256": getattr(doc, "sha256", None)}

    def _sha_of(self, filename: str) -> str | None:
        return next((getattr(d, "sha256", None) for d in self.docs if d.filename == filename), None)

    def list_manuals(self) -> dict[str, Any]:
        """The manuals behind every answer: model, file, sha256, pages, fault codes found, reader version."""
        return manifest(self.docs)

    # ---- tools ----------------------------------------------------------------------------------------

    def lookup_fault_code(self, code: int | str, model: str | None = None) -> dict[str, Any]:
        """Exact fault-code lookup in the manuals (a number like 39 or a letter code like F0234), optionally only
        the manual of one model label."""
        matches = []
        for doc in self.docs:
            label = config.model_of(doc.filename)
            if model and label.lower() != model.lower():
                continue
            for rec in doc.data.get("fault_codes") or []:
                if record_matches(rec, code):
                    matches.append({"model": label, "code": rec.get("code"), "name": rec.get("name"),
                                    "severity": rec.get("severity"), "causes": rec.get("causes"),
                                    "measures": rec.get("measures") or [],
                                    "source": self.source(doc, rec.get("page"), rec.get("page_end"))})
        note = None if matches else "Code not found in the manuals" + (f" for model {model}" if model else "")
        if len(matches) > 1:
            note = "The same number exists in several manuals: pick by model."
        return {"code": code, "matches": matches, "note": note}

    def search_manual(self, query: str, top_k: int = 5, model: str | None = None) -> dict[str, Any]:
        """Search the manuals (hybrid search plus exact fault-code lookup), optionally only the manual of one
        model label (see list_manuals). Passages are shortened."""
        filters: dict[str, Any] = {}
        if model:
            files = [d.filename for d in self.docs if config.model_of(d.filename).lower() == model.lower()]
            if not files:
                known = sorted({config.model_of(d.filename) for d in self.docs})
                return {"query": query, "hits": [], "note": f"No manual for model {model}. Known models: {known}"}
            filters["files"] = files
        hits = self.retriever.search(query, top_k=max(1, min(top_k, 10)), **filters)
        return {"query": query, "hits": [
            {"file": h["file"], "page": h.get("page"), "kind": h.get("kind"), "text": h["text"][:TEXT_CHARS],
             "sha256": self._sha_of(h["file"])} for h in hits]}

    def ask_manual(self, question: str) -> dict[str, Any]:
        """Answer from the manuals with citations checked in code. One paid model call, only if the
        operator allowed external calls (see .env.example); the agent cannot allow it."""
        try:
            a = answer(question, self.retriever)
        except ExternalCallBlocked as e:
            return {"question": question, "blocked": True, "note": f"External model call is not allowed: {e}"}
        return {"question": question, "answer": a.answer, "found": a.found, "verified": a.verified,
                "citations": [{"file": c.file, "page": c.page, "verified": c.verified, "quote": c.quote,
                               "sha256": self._sha_of(c.file)} for c in a.citations]}

    def _incidents(self) -> tuple[list[dict[str, Any]], int]:
        report = read_log(self.log_path)
        return group(report.events), len(report.events)

    def get_incidents(self, repeated_only: bool = False, model: str | None = None, site: str | None = None,
                      asset_id: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Incidents from the fault log: alarm storms merged, repeats marked, priority and respond-by time."""
        incidents, n_events = self._incidents()
        chosen = filter_incidents(incidents, repeated_only=repeated_only,
                                  models=[model] if model else None, sites=[site] if site else None)
        if asset_id:
            chosen = [i for i in chosen if i["asset_id"] == asset_id]
        rows = []
        for inc in chosen[max(0, offset): max(0, offset) + max(1, min(limit, MAX_ROWS))]:
            wo = build(self.docs, inc)
            rows.append({"id": wo.id, "ts": inc["ts"], "asset_id": inc["asset_id"], "site": inc["site"],
                         "model": inc["model"], "fault_code": inc["fault_code"], "occurrences": inc["occurrences"],
                         "repeated": inc["repeated"], "prior_incidents": inc["prior_incidents"],
                         "priority": wo.priority, "priority_reasons": wo.priority_reasons,
                         "respond_by": wo.respond_by})
        stats = {"events": n_events, "incidents": len(incidents),
                 "repeats": sum(1 for i in incidents if i["repeated"])}
        return {"summary": stats, "matching": len(chosen), "shown": len(rows),
                "incidents": rows}

    def get_work_order(self, order_id: str) -> dict[str, Any]:
        """A saved work order (with its status), else a fresh draft built from the incident with this id."""
        if not SAFE_ID.match(order_id):
            return {"id": order_id, "note": "Invalid id."}
        saved = load_order(order_id, self.orders_dir)
        if saved:
            return {"saved": True, "work_order": saved.model_dump()}
        incidents, _ = self._incidents()
        for inc in incidents:
            if work_order_id(inc["ts"], inc["asset_id"], int(inc["fault_code"])) == order_id:
                wo = build(self.docs, inc, self.retriever)
                return {"saved": False, "work_order": wo.model_dump()}
        return {"id": order_id, "note": "No work order or incident with this id."}


    def propose_work_order(self, order_id: str) -> dict[str, Any]:
        """Save the work order of an incident as a DRAFT for a person to review. This is the only write an agent
        can do. It cannot approve, assign or close an order, and an order that is already saved stays as it is."""
        if not SAFE_ID.match(order_id):
            return {"id": order_id, "note": "Invalid id."}
        incidents, _ = self._incidents()
        for inc in incidents:
            if work_order_id(inc["ts"], inc["asset_id"], int(inc["fault_code"])) == order_id:
                wo, created = save_draft(build(self.docs, inc, self.retriever), self.orders_dir)
                return {"created": created, "status": wo.status, "approved_by": wo.approved_by,
                        "next": "A person approves it: om-copilot approve " + order_id + " --by NAME, or the button "
                                "on the Work order tab of the web page. The agent cannot approve."}
        return {"id": order_id, "note": "No incident with this id."}


def build_server(tools: Tools | None = None) -> Any:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tools = tools or Tools()
    server = MCPServer("om-copilot", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True)
    for fn in (tools.list_manuals, tools.lookup_fault_code, tools.search_manual, tools.ask_manual, tools.get_incidents,
               tools.get_work_order):
        server.tool(annotations=read_only)(fn)
    # the only write: a draft file; idempotent, not destructive; there is no approve tool
    server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))(
        tools.propose_work_order)
    server.resource("manuals://manifest", name="manuals-manifest", mime_type="application/json",
                    description="Manuals behind the answers: model, file, sha256, pages, fault codes, reader version")(
        lambda: json.dumps(manifest(tools.docs), ensure_ascii=False))
    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")  # stderr
    build_server().run("stdio")


if __name__ == "__main__":
    main()
