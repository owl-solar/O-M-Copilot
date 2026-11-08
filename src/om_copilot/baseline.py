"""Baseline for comparison: the same model gets the WHOLE text of all manuals in its context
(what "just keep the manuals and ask an AI" means), instead of the 8 retrieved passages.

Fair to the baseline: the text is the same page-labelled text our own pipeline reads (design-ingest), the
model, answer tool and grading are the same. Every quote is checked against the text of the page it names.
The manuals are sent as one cached block, so only the first call pays the full price.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from typing import Any

from design_ingest import Document
from design_ingest.ask import DEFAULT_MODEL, Answer, Citation
from design_ingest.llm import guarded_create
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SYSTEM = (
    "You answer questions about PV inverter manuals for an engineer. The full text of the manuals follows, "
    "page by page, each page marked '=== FILE: name | PAGE n ==='. Use ONLY this text; never use outside "
    "knowledge. If the manuals do not contain the answer, set found=false and say what is missing. Every "
    "statement of fact needs a citation: the file name, the page number and a quote copied exactly from that "
    "page (at most 200 characters, no rewording). Keep the answer short and keep the units."
)

# USD per million tokens for claude-haiku-5-5 above 100K prompt tokens; cache write 1.25x, cache read 0.1x of
# the input price. An estimate for the spending cap, not billing.
PRICE_IN, PRICE_OUT = 0.50, 2.50


class _Cite(BaseModel):
    file: str = Field(description="file name exactly as in the page marker")
    page: int
    quote: str = Field(description="verbatim quote from that page, at most 200 characters")


class _Reply(BaseModel):
    answer: str
    found: bool = Field(description="false if the manuals do not contain the answer")
    citations: list[_Cite] = Field(description="empty when found is false")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def build_context(docs: list[Document]) -> tuple[str, dict[tuple[str, int], str]]:
    """All text, page by page, plus the text per (file, page) for checking quotes."""
    pages: dict[tuple[str, int], list[str]] = defaultdict(list)
    for doc in docs:
        for c in doc.chunks:
            if c.page:
                pages[(doc.filename, c.page)].append(c.text)
    page_text = {k: "\n".join(v) for k, v in sorted(pages.items())}
    blocks = [f"=== FILE: {f} | PAGE {p} ===\n{t}" for (f, p), t in page_text.items()]
    return "\n\n".join(blocks), page_text


def cost_usd(usage: Any) -> float:
    inp = getattr(usage, "input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (inp + 1.25 * write + 0.1 * read) * PRICE_IN / 1e6 + out * PRICE_OUT / 1e6


def ask_baseline(question: str, context: str, page_text: dict[tuple[str, int], str], *, client: Any,
                 allow_external: bool | None = None, model: str | None = None,
                 max_tokens: int = 1024) -> tuple[Answer, Any]:
    """Returns the Answer (graded like ours) and the raw usage of the call."""
    model = model or os.environ.get("DESIGN_INGEST_MODEL", DEFAULT_MODEL)
    tool = {"name": "reply", "description": "Give the answer with checked citations.",
            "input_schema": _Reply.model_json_schema()}
    resp = guarded_create(
        client, purpose="baseline", file="(all manuals)", sha256="", kind="text", amount=len(context),
        allow_external=allow_external, model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": SYSTEM},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}],
        tools=[tool], tool_choice={"type": "tool", "name": "reply"},
        messages=[{"role": "user", "content": f"Question: {question}"}],
    )
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise RuntimeError(f"model returned no structured answer (stop_reason={resp.stop_reason})")
    reply = _Reply.model_validate(block.input)
    cites = [
        Citation(n=0, file=c.file, page=c.page, kind=None, quote=c.quote,
                 verified=bool(c.quote.strip()) and _norm(c.quote) in _norm(page_text.get((c.file, c.page), "")))
        for c in reply.citations
    ]
    answer = Answer(question=question, answer=reply.answer, found=reply.found,
                    verified=any(c.verified for c in cites), citations=cites, model=model)
    return answer, resp.usage
