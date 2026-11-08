"""Draft work order from a fault-log record. No LLM: the steps are the manual's own "measures",
word for word, each with file and page, so nothing in the order is invented.

JSON (``WorkOrder.model_dump_json``) is the source of truth, ``to_markdown`` is the view.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from design_ingest import Document
from pydantic import BaseModel

from . import config
from .retrieval import _ranges
from .synth import family_of

log = logging.getLogger(__name__)

DEFAULT_PRIORITY = "P3"  # priority table: severity printed in the manual -> P1..P3, see config.py
SAFETY_QUERY = "safety warning danger power off disconnect before maintenance"
SAFETY_EXCERPT_CHARS = 400
SAFETY_LABEL = re.compile(r"\b(danger|warning|caution)\b", re.IGNORECASE)
NUMBERING = re.compile(r"^\d+\.\s+")


def _clean_excerpt(text: str) -> str:
    """Markdown table borders out, whitespace squeezed, cut at a word boundary."""
    text = re.sub(r"^\|[-| ]+\|$", " ", text, flags=re.MULTILINE).replace("|", " ")
    text = re.sub("­\\s*", "", text)  # soft hyphen + line break inside a word ("oper­ ators")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= SAFETY_EXCERPT_CHARS:
        return text
    return text[:SAFETY_EXCERPT_CHARS].rsplit(" ", 1)[0] + " ..."


def _safety(retriever: Any, doc: Document) -> list[Item]:
    """Warning passages of the same manual: only passages with a DANGER/WARNING/CAUTION label, no repeats."""
    items: list[Item] = []
    seen: set[str] = set()
    for h in retriever.search(SAFETY_QUERY, top_k=8, files=[doc.filename]):
        if h.get("kind") == "fault_code" or not SAFETY_LABEL.search(h["text"]):
            continue
        text = _clean_excerpt(h["text"])
        if text in seen:
            continue
        seen.add(text)
        items.append(Item(n=len(items) + 1, text=text, source=Source(file=h["file"], page=h.get("page"))))
        if len(items) == 2:
            break
    return items


class Source(BaseModel):
    file: str
    page: int | None = None
    page_end: int | None = None


class Item(BaseModel):
    n: int
    text: str
    source: Source


class WorkOrder(BaseModel):
    draft: bool = True
    ts: str
    asset_id: str
    site: str
    model: str
    fault_code: int
    found: bool
    fault_name: str | None = None
    severity: str | None = None
    priority: str = DEFAULT_PRIORITY
    steps: list[Item] = []
    safety: list[Item] = []
    note: str | None = None


def find_record(docs: Iterable[Document], model: str, code: int) -> tuple[Document, dict[str, Any]] | None:
    """The fault_codes record of the manual that belongs to ``model`` and covers ``code``."""
    for doc in docs:
        if family_of(doc.filename) != model:
            continue
        for rec in doc.data.get("fault_codes") or []:
            if any(lo <= code <= hi for lo, hi in _ranges(rec)):
                return doc, rec
    return None


def priority_of(severity: str | None) -> str:
    return config.priority_of(severity)


def _source(doc: Document, rec: dict[str, Any]) -> Source:
    page, end = rec.get("page"), rec.get("page_end")
    return Source(file=doc.filename, page=page, page_end=end if end and end != page else None)


def build(docs: list[Document], event: dict[str, Any], retriever: Any | None = None) -> WorkOrder:
    """Work order for one log record (keys ts, asset_id, model, fault_code, site)."""
    base = {"ts": event["ts"], "asset_id": event["asset_id"], "site": event["site"], "model": event["model"],
            "fault_code": int(event["fault_code"])}
    found = find_record(docs, base["model"], base["fault_code"])
    if found is None:
        log.warning("fault %s of %s not found in the manuals", base["fault_code"], base["model"])
        return WorkOrder(**base, found=False,
                         note="Code not found in the manual for this model: no steps. Check the code and the model.")
    doc, rec = found
    src = _source(doc, rec)
    steps = [Item(n=i, text=NUMBERING.sub("", m), source=src) for i, m in enumerate(rec.get("measures") or [], 1)]
    safety = _safety(retriever, doc) if retriever is not None else []
    severity = rec.get("severity")
    return WorkOrder(**base, found=True, fault_name=rec.get("name"), severity=severity,
                     priority=priority_of(severity), steps=steps, safety=safety,
                     note=None if steps else "The manual lists no corrective measures for this code.")


def _cite(s: Source) -> str:
    pages = f"pp.{s.page}-{s.page_end}" if s.page_end else f"p.{s.page}"
    return f"[{s.file}, {pages}]"


def to_markdown(wo: WorkOrder) -> str:
    head = f"# Work order (DRAFT): {wo.asset_id}, fault {wo.fault_code}"
    lines = [head, "",
             f"- Asset: {wo.asset_id} ({wo.model}), site {wo.site}",
             f"- Reported: {wo.ts}",
             f"- Fault: {wo.fault_code}" + (f" {wo.fault_name}" if wo.fault_name else ""),
             f"- Priority: {wo.priority}" + (f" (manual severity: {wo.severity})" if wo.severity else "")]
    if wo.note:
        lines += ["", f"> {wo.note}"]
    if wo.safety:
        lines += ["", "## Safety (excerpts from the manual)"]
        lines += [f"{i.n}. {i.text} {_cite(i.source)}" for i in wo.safety]
    if wo.steps:
        lines += ["", "## Steps"]
        lines += [f"{i.n}. {i.text} {_cite(i.source)}" for i in wo.steps]
    return "\n".join(lines) + "\n"
