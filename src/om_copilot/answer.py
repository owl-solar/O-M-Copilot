"""Answer a free question: our retrieval (code lookup + search) -> design_ingest.ask.answer_from_hits.

The model sees only the retrieved passages and must quote them word for word; design-ingest checks every
quote in code (``verified``). The external call is blocked unless allowed (see .env.example).
"""
from __future__ import annotations

import logging
from typing import Any

from design_ingest.ask import Answer, answer_from_hits

from .retrieval import Retriever

log = logging.getLogger(__name__)

TOP_K = 8


def answer(question: str, retriever: Retriever, *, top_k: int = TOP_K, model: str | None = None,
           client: Any = None, allow_external: bool | None = None) -> Answer:
    hits = retriever.search(question, top_k=top_k)
    return answer_from_hits(question, hits, model=model, client=client, allow_external=allow_external)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def grade(q: dict[str, Any], a: Answer) -> dict[str, Any]:
    """Checks without an LLM judge.

    answerable: ``found``, every citation verified, an expected (file, page) is cited, all ``must_contain``
    words (local file, optional) appear in the answer or in a verified quote. Unanswerable: the model must say ``found`` = false.
    """
    if not q["answerable"]:
        return {"id": q["id"], "ok": not a.found, "refused": not a.found}
    quotes = " ".join(c.quote for c in a.citations if c.verified)
    text = _norm(a.answer + " " + quotes)
    cited = any(c.file == q["expected_file"] and c.page in q["expected_pages"] for c in a.citations)
    words = q.get("must_contain")
    contains = all(_norm(w) in text for w in words) if words else None  # None: no words, not graded
    all_verified = bool(a.citations) and all(c.verified for c in a.citations)
    return {"id": q["id"], "ok": a.found and all_verified and cited and contains is not False, "found": a.found,
            "all_verified": all_verified, "cited_expected": cited, "must_contain": contains}


def summarize(grades: list[dict[str, Any]]) -> dict[str, float | None]:
    """Shares over answerable questions, plus the refusal share over the unanswerable ones."""
    ans = [g for g in grades if "found" in g]
    unans = [g for g in grades if "refused" in g]

    def share(key: str) -> float | None:
        graded = [g for g in ans if g[key] is not None]
        return sum(1 for g in graded if g[key]) / len(graded) if graded else None

    return {"n_answerable": len(ans), "n_unanswerable": len(unans),
            "found": share("found"), "all_citations_verified": share("all_verified"),
            "cited_expected_page": share("cited_expected"), "must_contain": share("must_contain"),
            "refused_unanswerable": sum(1 for g in unans if g["refused"]) / len(unans) if unans else 0.0}
