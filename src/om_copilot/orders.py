"""Saved work orders and the one write that needs a person: approval.

The agent side (MCP) may only call ``save_draft``. ``approve`` is used by the command line and the Streamlit
button, never by an MCP tool: a client can answer an MCP elicitation by itself, a button or a terminal cannot
be pressed by the model.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .workorder import WorkOrder, to_markdown

DEFAULT_ORDERS = "data/workorders"
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def orders_dir() -> Path:
    return Path(os.environ.get("OM_COPILOT_WORKORDERS", DEFAULT_ORDERS))


def _path(order_id: str, folder: Path) -> Path:
    if not SAFE_ID.match(order_id):
        raise ValueError(f"invalid work order id: {order_id!r}")
    return folder / f"{order_id}.json"


def load(order_id: str, folder: Path | None = None) -> WorkOrder | None:
    path = _path(order_id, folder or orders_dir())
    return WorkOrder.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def _write(wo: WorkOrder, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{wo.id}.json").write_text(wo.model_dump_json(indent=2), encoding="utf-8")
    (folder / f"{wo.id}.md").write_text(to_markdown(wo), encoding="utf-8")


def save_draft(wo: WorkOrder, folder: Path | None = None) -> tuple[WorkOrder, bool]:
    """Store ``wo`` as a draft. An order that is already saved is never overwritten (an approved one must not
    slip back to draft). Returns the stored order and whether this call created it."""
    folder = folder or orders_dir()
    existing = load(wo.id, folder)
    if existing is not None:
        return existing, False
    draft = wo.model_copy(update={"status": "draft", "approved_by": None, "assignee": None, "work_done": None})
    _write(draft, folder)
    return draft, True


def approve(order_id: str, approved_by: str, folder: Path | None = None) -> WorkOrder:
    """A person approves a saved draft. Raises ValueError if there is no such draft or the name is empty."""
    folder = folder or orders_dir()
    name = approved_by.strip()
    if not name:
        raise ValueError("approved_by must be the name of the person who approves")
    wo = load(order_id, folder)
    if wo is None:
        raise ValueError(f"no saved work order {order_id}: save it as a draft first")
    if wo.status != "draft":
        raise ValueError(f"work order {order_id} is already {wo.status}")
    wo = wo.model_copy(update={"status": "approved", "approved_by": name})
    _write(wo, folder)
    return wo
