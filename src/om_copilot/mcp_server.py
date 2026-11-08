"""MCP server (stdio): the existing functions as tools for an agent. No logic of its own.

Run with ``om-copilot mcp`` (needs the extra: ``pip install -e .[mcp]``). The tool functions are plain methods
of ``Tools`` so tests call them directly; ``mcp`` is imported only when the server is built.

Every answer that comes from a manual carries its source: file, page and sha256 of that exact file.
stdout belongs to the protocol: log to stderr only, never print.
"""
from __future__ import annotations

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
from .retrieval import Retriever, _ranges
from .workorder import WorkOrder, build, work_order_id

log = logging.getLogger(__name__)

DEFAULT_LOG = "data/synth/faults.jsonl"
DEFAULT_ORDERS = "data/workorders"
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
        self.orders_dir = Path(orders_dir or os.environ.get("OM_COPILOT_WORKORDERS", DEFAULT_ORDERS))
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

    # ---- tools ----------------------------------------------------------------------------------------

    def lookup_fault_code(self, code: int, model: str | None = None) -> dict[str, Any]:
        """Exact fault-code lookup in the manuals (optionally only the manual of one model label)."""
        matches = []
        for doc in self.docs:
            label = config.model_of(doc.filename)
            if model and label.lower() != model.lower():
                continue
            for rec in doc.data.get("fault_codes") or []:
                if any(lo <= code <= hi for lo, hi in _ranges(rec)):
                    matches.append({"model": label, "code": rec.get("code"), "name": rec.get("name"),
                                    "severity": rec.get("severity"), "causes": rec.get("causes"),
                                    "measures": rec.get("measures") or [],
                                    "source": self.source(doc, rec.get("page"), rec.get("page_end"))})
        note = None if matches else "Code not found in the manuals" + (f" for model {model}" if model else "")
        if len(matches) > 1:
            note = "The same number exists in several manuals: pick by model."
        return {"code": code, "matches": matches, "note": note}

    def search_manual(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """Search the manuals (hybrid search plus exact fault-code lookup). Passages are shortened."""
        hits = self.retriever.search(query, top_k=max(1, min(top_k, 10)))
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
                      asset_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Incidents from the fault log: alarm storms merged, repeats marked, priority and respond-by time."""
        incidents, n_events = self._incidents()
        chosen = filter_incidents(incidents, repeated_only=repeated_only,
                                  models=[model] if model else None, sites=[site] if site else None)
        if asset_id:
            chosen = [i for i in chosen if i["asset_id"] == asset_id]
        rows = []
        for inc in chosen[: max(1, min(limit, MAX_ROWS))]:
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

    def _saved_order(self, order_id: str) -> WorkOrder | None:
        path = self.orders_dir / f"{order_id}.json"
        if not path.is_file():
            return None
        return WorkOrder.model_validate_json(path.read_text(encoding="utf-8"))

    def get_work_order(self, order_id: str) -> dict[str, Any]:
        """A saved work order (with its status), else a fresh draft built from the incident with this id."""
        if not SAFE_ID.match(order_id):
            return {"id": order_id, "note": "Invalid id."}
        saved = self._saved_order(order_id)
        if saved:
            return {"saved": True, "work_order": saved.model_dump()}
        incidents, _ = self._incidents()
        for inc in incidents:
            if work_order_id(inc["ts"], inc["asset_id"], int(inc["fault_code"])) == order_id:
                wo = build(self.docs, inc, self.retriever)
                return {"saved": False, "work_order": wo.model_dump()}
        return {"id": order_id, "note": "No work order or incident with this id."}


def build_server(tools: Tools | None = None) -> Any:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    tools = tools or Tools()
    server = MCPServer("om-copilot", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True)
    for fn in (tools.lookup_fault_code, tools.search_manual, tools.ask_manual, tools.get_incidents,
               tools.get_work_order):
        server.tool(annotations=read_only)(fn)
    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")  # stderr
    build_server().run("stdio")


if __name__ == "__main__":
    main()
