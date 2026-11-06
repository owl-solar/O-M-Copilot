"""Synthetic fault log: fictional plant, real fault codes from the local manuals.

Only code numbers and severity are taken from the manuals, never their text. Frequencies follow a
Pareto-like law (a few codes give most events) and part of the events come as clusters (several
inverters of one site, same code, within minutes: lightning, grid trouble). Same seed -> same log.
"""
from __future__ import annotations

import json
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from design_ingest import Document

from .retrieval import _ranges

# Product family label per manual: matched by a word in the file name (case-insensitive).
FAMILIES = (("sg3", "SG350HX-20"), ("alarm reference", "SUN2000"), ("sccp", "Sunny Central"))
SITES = ("Site A", "Site B", "Site C", "Site D")
START = datetime(2026, 1, 1)
DAYS = 180


@dataclass(frozen=True)
class CodeRef:
    model: str
    code: int
    severity: str


@dataclass(frozen=True)
class FaultEvent:
    ts: str
    asset_id: str
    model: str
    fault_code: int
    severity: str
    site: str


def family_of(filename: str) -> str:
    name = filename.lower()
    for key, label in FAMILIES:
        if key in name:
            return label
    return Path(filename).stem


def code_pool(docs: Iterable[Document]) -> list[CodeRef]:
    """One entry per numeric code of every manual (first number of a range)."""
    pool: list[CodeRef] = []
    for doc in docs:
        model = family_of(doc.filename)
        for rec in doc.data.get("fault_codes") or []:
            for lo, _hi in _ranges(rec):
                pool.append(CodeRef(model, lo, str(rec.get("severity") or "unknown")))
    return pool


def generate(pool: list[CodeRef], n: int = 1000, seed: int = 42, cluster_share: float = 0.25) -> list[FaultEvent]:
    """``n`` events sorted by time. ``pool`` must not be empty."""
    if not pool:
        raise ValueError("empty code pool: no fault codes found in the manuals")
    rng = random.Random(seed)
    models = sorted({c.model for c in pool})
    # fixed fleet: 12 inverters per model (INV-01...), ids unique across models, spread over the sites
    fleet = {m: [f"INV-{k * 12 + i + 1:02d}" for i in range(12)] for k, m in enumerate(models)}
    site_of = {(m, a): SITES[(i + k) % len(SITES)] for k, m in enumerate(models) for i, a in enumerate(fleet[m])}

    ranked = pool[:]
    rng.shuffle(ranked)
    weights = [1 / (r + 1) ** 1.1 for r in range(len(ranked))]  # Pareto-like: rank 1 is the most frequent

    def moment() -> datetime:
        return START + timedelta(seconds=rng.randrange(DAYS * 86400))

    events: list[FaultEvent] = []

    def add(ref: CodeRef, asset: str, when: datetime) -> None:
        events.append(FaultEvent(when.isoformat(timespec="seconds"), asset, ref.model, ref.code, ref.severity, site_of[(ref.model, asset)]))

    n_cluster = int(n * cluster_share)
    while len(events) < n_cluster:
        ref = rng.choices(ranked, weights)[0]
        site = rng.choice(SITES)
        members = [a for a in fleet[ref.model] if site_of[(ref.model, a)] == site]
        if not members:
            continue
        t0 = moment()
        for asset in rng.sample(members, k=min(len(members), rng.randint(3, 6))):
            add(ref, asset, t0 + timedelta(seconds=rng.randrange(1800)))
    del events[n_cluster:]
    while len(events) < n:
        ref = rng.choices(ranked, weights)[0]
        add(ref, rng.choice(fleet[ref.model]), moment())
    return sorted(events, key=lambda e: (e.ts, e.asset_id))


def write_jsonl(events: Iterable[FaultEvent], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
            count += 1
    return count
