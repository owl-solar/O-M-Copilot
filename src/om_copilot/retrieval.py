"""Retrieval: exact fault-code lookup first, then design-ingest search (BM25 or hybrid).

Embeddings blur numbers ("450" ~ "540"), so a code named in the question is looked up directly in
``Document.data["fault_codes"]`` (ranges such as 448-479 included) and put at the top of the results.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from design_ingest import Document
from design_ingest.search import build_index

# A number counts as a fault code only after a cue word: "fault 39", "alarm code 2062", "error 3504".
# Bare numbers ("25 minutes", "SG350HX", "0.01 A") are not codes.
_CODE_CUE = re.compile(
    r"\b(?:fault|alarm|error|event|code)s?(?:\s+(?:code|number|no\.?|id))?\s*#?\s*(\d{1,5})\b", re.IGNORECASE
)


# Codes with letters (F0234, E-306): a token counts as a code when it is exactly a code of some manual's table,
# so "SG350HX" is never taken for one. Numeric codes need a cue word, see above.
_LABEL = re.compile(r"^[A-Za-z]{1,3}[-_]?\d{1,6}$")
_LABEL_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,3}[-_]?\d{1,6}(?![A-Za-z0-9])")


def _norm_label(s: str) -> str:
    return re.sub(r"[-_\s]", "", s).upper()


@dataclass(frozen=True)
class CodeEntry:
    file: str
    lo: int
    hi: int
    page: int | None
    text: str
    label: str | None = None  # normalised letter code, e.g. "F0234"; lo = hi = -1 then


def _ranges(record: dict[str, Any]) -> list[tuple[int, int]]:
    """Numeric ranges of a fault_codes record, or [] if the 'code' is not a number (e.g. a footnote)."""
    ranges = record.get("code_ranges")
    if ranges:
        return [(int(a), int(b)) for a, b in ranges]
    code = str(record.get("code", "")).strip()
    return [(int(code), int(code))] if code.isdigit() else []


def _labels(record: dict[str, Any]) -> list[str]:
    """The record's code if it has letters (F0234); [] for numeric codes, ranges and footnotes."""
    code = str(record.get("code", "")).strip()
    return [_norm_label(code)] if _LABEL.match(code) else []


def record_matches(record: dict[str, Any], code: int | str) -> bool:
    """Does the fault_codes record cover ``code`` (a number, a numeric string or a letter code)?"""
    text = str(code).strip()
    if text.isdigit():
        return any(lo <= int(text) <= hi for lo, hi in _ranges(record))
    return _norm_label(text) in _labels(record)


def _record_text(record: dict[str, Any]) -> str:
    head = f"Fault code {record['code']}"
    if record.get("name"):
        head += f": {record['name']}"
    measures = record.get("measures") or []
    return head + (". Corrective measures: " + " ".join(measures) if measures else "")


def build_code_table(docs: Iterable[Document]) -> list[CodeEntry]:
    table: list[CodeEntry] = []
    for doc in docs:
        for rec in doc.data.get("fault_codes") or []:
            text = _record_text(rec)
            for lo, hi in _ranges(rec):
                table.append(CodeEntry(doc.filename, lo, hi, rec.get("page"), text))
            for label in _labels(rec):
                table.append(CodeEntry(doc.filename, -1, -1, rec.get("page"), text, label))
    return table


def codes_in_question(question: str) -> list[int]:
    return [int(m) for m in _CODE_CUE.findall(question)]


def find_codes(question: str, table: list[CodeEntry]) -> list[CodeEntry]:
    found: list[CodeEntry] = []
    for n in codes_in_question(question):
        for e in table:
            if e.label is None and e.lo <= n <= e.hi and e not in found:
                found.append(e)
    for token in _LABEL_TOKEN.findall(question):
        for e in table:
            if e.label == _norm_label(token) and e not in found:
                found.append(e)
    return found


class Retriever:
    """``search(query, top_k)`` returns hits like design_ingest.search.Index.search."""

    def __init__(self, docs: list[Document], mode: str = "bm25", code_lookup: bool = True):
        self.index = build_index(docs, mode=mode)
        self.table = build_code_table(docs) if code_lookup else []

    def search(self, query: str, top_k: int = 8, **filters: Any) -> list[dict[str, Any]]:
        exact = [
            {"file": e.file, "page": e.page, "kind": "fault_code", "text": e.text, "score": float("inf"),
             "source": "code_lookup"}
            for e in find_codes(query, self.table)
            if not filters.get("files") or e.file in filters["files"]
        ]
        rest = self.index.search(query, top_k=top_k + len(exact), **filters)
        seen = {(h["file"], h["page"], h["text"]) for h in exact}
        merged = exact + [h for h in rest if (h["file"], h.get("page"), h["text"]) not in seen]
        return merged[:top_k]
