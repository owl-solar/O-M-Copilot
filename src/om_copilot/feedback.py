"""Technician feedback: for a step of a work order "helped" or "did not work".

An append-only jsonl log (``data/feedback.jsonl``, env OM_COPILOT_FEEDBACK; git-ignored, it is field data).
One record per verdict; if a technician changes their mind, the LAST verdict for (work order, step) counts.
The rate "helped / (helped + failed)" is the first quality number that comes from the field, not from our own
questions. It measures usefulness of the steps, not whether the manual is right, and it means little until
a code has some verdicts: the report always shows n.
"""
from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .workorder import WorkOrder

VERDICTS = ("helped", "failed")
REVIEW_MIN_VERDICTS = 3  # a code is flagged for review with at least this many verdicts ...
REVIEW_BELOW_RATE = 0.5  # ... and a helped rate below this


def log_path() -> Path:
    return Path(os.environ.get("OM_COPILOT_FEEDBACK", "data/feedback.jsonl"))


def record(wo: WorkOrder, step_n: int, verdict: str, note: str | None = None, who: str | None = None,
           path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Append one verdict. ``step_n`` must be a step of the order."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    step = next((s for s in wo.steps if s.n == step_n), None)
    if step is None:
        raise ValueError(f"work order {wo.id} has no step {step_n} (steps: 1-{len(wo.steps)})")
    rec = {"ts": (now or datetime.now()).isoformat(timespec="seconds"), "wo_id": wo.id, "model": wo.model,
           "fault_code": wo.fault_code, "step_n": step_n, "page": step.source.page, "verdict": verdict,
           "note": note or "", "who": who or ""}
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or log_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _rate(helped: int, failed: int) -> float | None:
    return helped / (helped + failed) if helped + failed else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Overall rate and a row per (model, code), worst first. The last verdict per (order, step) counts."""
    last: dict[tuple[str, int], dict[str, Any]] = {}
    for r in records:
        last[(r["wo_id"], r["step_n"])] = r
    per_code: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: {"helped": 0, "failed": 0})
    for r in last.values():
        per_code[(r["model"], r["fault_code"])][r["verdict"]] += 1
    rows = []
    for (model, code), c in per_code.items():
        n = c["helped"] + c["failed"]
        rate = _rate(c["helped"], c["failed"])
        rows.append({"model": model, "fault_code": code, "n": n, "helped": c["helped"], "failed": c["failed"],
                     "rate": rate, "review": n >= REVIEW_MIN_VERDICTS and rate is not None and rate < REVIEW_BELOW_RATE})
    rows.sort(key=lambda r: (r["rate"], -r["n"]))
    helped = sum(r["helped"] for r in rows)
    failed = sum(r["failed"] for r in rows)
    return {"verdicts": helped + failed, "helped": helped, "failed": failed, "rate": _rate(helped, failed),
            "codes": rows, "to_review": [r for r in rows if r["review"]]}


def synthetic(orders: list[WorkOrder], seed: int = 42, per_order: float = 0.6) -> list[dict[str, Any]]:
    """DEMO ONLY: invented verdicts for existing orders, so the report has something to show.

    Marked ``synthetic`` and meant for a separate file. It says nothing about the real quality of any manual.
    """
    rng = random.Random(seed)
    weak = {(o.model, o.fault_code) for o in rng.sample(orders, k=min(len(orders), max(1, len(orders) // 5)))}
    out = []
    for o in orders:
        for s in o.steps:
            if rng.random() > per_order:
                continue
            p_help = 0.3 if (o.model, o.fault_code) in weak else 0.8
            out.append({"ts": o.ts, "wo_id": o.id, "model": o.model, "fault_code": o.fault_code, "step_n": s.n,
                        "page": s.source.page, "verdict": "helped" if rng.random() < p_help else "failed",
                        "note": "", "who": "synthetic", "synthetic": True})
    return out
