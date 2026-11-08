"""Work orders out: one flat CSV row per order for a maintenance system (CMMS), plus the full JSON.

The columns are generic, not the import format of one product; map them in the target system.
Status, assignee, approval and work done are empty or "draft": people fill them in, this tool does not.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .workorder import Item, WorkOrder

COLUMNS = (
    "id", "status", "priority", "priority_reasons", "respond_by", "reported", "asset_id", "site", "model",
    "rated_kw", "fault_code", "fault_name", "manual_severity", "alarms_merged", "repeat", "steps", "safety",
    "manual_file", "manual_pages", "manual_sha256", "assignee", "approved_by", "work_done", "note",
)


def load_orders(folder: str | Path) -> tuple[list[WorkOrder], list[str]]:
    """Valid work orders and the names of files that are not (for example an older format)."""
    orders: list[WorkOrder] = []
    skipped: list[str] = []
    for p in sorted(Path(folder).glob("*.json")):
        try:
            orders.append(WorkOrder.model_validate_json(p.read_text(encoding="utf-8")))
        except ValueError:
            skipped.append(p.name)
    return orders, skipped


def _text(items: list[Item]) -> str:
    return "\n".join(
        f"{i.n}. {i.text} [p.{i.source.page}{f'-{i.source.page_end}' if i.source.page_end else ''}]" for i in items)


def _pages(wo: WorkOrder) -> str:
    pages: set[int] = set()
    for i in (*wo.steps, *wo.safety):
        pages.update(p for p in (i.source.page, i.source.page_end) if p)
    return ",".join(str(p) for p in sorted(pages))


def to_row(wo: WorkOrder) -> dict[str, object]:
    files = {i.source.file for i in (*wo.steps, *wo.safety)}
    return {
        "id": wo.id, "status": wo.status, "priority": wo.priority, "priority_reasons": "; ".join(wo.priority_reasons),
        "respond_by": wo.respond_by or "", "reported": wo.ts, "asset_id": wo.asset_id, "site": wo.site,
        "model": wo.model, "rated_kw": wo.rated_kw or "", "fault_code": wo.fault_code, "fault_name": wo.fault_name or "",
        "manual_severity": wo.severity or "", "alarms_merged": wo.occurrences, "repeat": "yes" if wo.repeated else "no",
        "steps": _text(wo.steps), "safety": _text(wo.safety), "manual_file": "; ".join(sorted(files)),
        "manual_pages": _pages(wo), "manual_sha256": wo.manual_sha256 or "", "assignee": wo.assignee or "",
        "approved_by": wo.approved_by or "", "work_done": wo.work_done or "", "note": wo.note or "",
    }


def write_csv(orders: list[WorkOrder], path: str | Path, delimiter: str = ",") -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel reads UTF-8 correctly
        out = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=delimiter)
        out.writeheader()
        for wo in orders:
            out.writerow(to_row(wo))
    return len(orders)


def write_json(orders: list[WorkOrder], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([wo.model_dump() for wo in orders], indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return len(orders)
