"""Retrieval evaluation: hit@k and MRR over eval/questions.json.

A hit means: one of the top-k passages comes from ``expected_file`` and from one of ``expected_pages``.
Ranks are counted over distinct (file, page) pairs, so several passages of one page count once.
Unanswerable questions are not part of the retrieval metrics.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

KS = (1, 3, 5)


MUST_CONTAIN_FILE = "data/must_contain.json"  # local, git-ignored: words taken from the manuals


def load_questions(path: str | Path, must_contain: str | Path | None = None) -> list[dict[str, Any]]:
    """Questions from the public file; ``must_contain`` words are merged in from a local file if it exists
    (``{"q01": ["word", ...]}``, env OM_COPILOT_MUST_CONTAIN). Without it the questions have no such field."""
    questions = json.loads(Path(path).read_text(encoding="utf-8"))
    local = Path(must_contain or os.environ.get("OM_COPILOT_MUST_CONTAIN", MUST_CONTAIN_FILE))
    if local.is_file():
        words = json.loads(local.read_text(encoding="utf-8"))
        for q in questions:
            if q["id"] in words:
                q["must_contain"] = words[q["id"]]
    return questions


def distinct_pages(hits: Iterable[dict[str, Any]]) -> list[tuple[str, int | None]]:
    seen: list[tuple[str, int | None]] = []
    for h in hits:
        key = (h["file"], h.get("page"))
        if key not in seen:
            seen.append(key)
    return seen


def rank_of_expected(ranked: list[tuple[str, int | None]], expected_file: str, expected_pages: list[int]) -> int | None:
    """1-based rank of the first expected (file, page), or None."""
    for i, (f, p) in enumerate(ranked, 1):
        if f == expected_file and p in expected_pages:
            return i
    return None


def score(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    if n == 0:
        return {"n": 0, **{f"hit@{k}": 0.0 for k in KS}, "mrr": 0.0}
    out: dict[str, float] = {"n": n}
    for k in KS:
        out[f"hit@{k}"] = sum(1 for r in ranks if r is not None and r <= k) / n
    out["mrr"] = sum(1 / r for r in ranks if r is not None) / n
    return out


def evaluate(index, questions: list[dict[str, Any]], top_k: int = 10) -> dict[str, Any]:
    """``index`` is a design_ingest.search.Index. Returns metrics overall, by type and misses."""
    ranks: dict[str, int | None] = {}
    for q in questions:
        if not q["answerable"]:
            continue
        ranked = distinct_pages(index.search(q["question"], top_k=top_k))
        ranks[q["id"]] = rank_of_expected(ranked, q["expected_file"], q["expected_pages"])
    by_type: dict[str, list[int | None]] = defaultdict(list)
    by_file: dict[str, list[int | None]] = defaultdict(list)
    for q in questions:
        if q["id"] in ranks:
            by_type[q["type"]].append(ranks[q["id"]])
            by_file[q["expected_file"]].append(ranks[q["id"]])
    return {
        "overall": score(list(ranks.values())),
        "by_type": {k: score(v) for k, v in by_type.items()},
        "by_file": {k: score(v) for k, v in by_file.items()},
        "misses": [q["id"] for q in questions if q["id"] in ranks and (ranks[q["id"]] is None or ranks[q["id"]] > 5)],
        "ranks": ranks,
    }
