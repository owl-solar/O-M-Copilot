"""Load the manuals from data/raw through design-ingest (the only place that reads documents)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from design_ingest import Document, ingest_dir

log = logging.getLogger(__name__)

DEFAULT_RAW_DIR = "data/raw"


def raw_dir() -> Path:
    return Path(os.environ.get("OM_COPILOT_RAW_DIR", DEFAULT_RAW_DIR))


def load_documents(folder: str | Path | None = None) -> list[Document]:
    """Parse every supported file in ``folder`` (default: OM_COPILOT_RAW_DIR or data/raw)."""
    folder = Path(folder) if folder else raw_dir()
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} not found: put the manuals there (see docs/sources.md)")
    docs = ingest_dir(folder)
    for path, msg in getattr(ingest_dir, "last_errors", []):
        log.warning("skipped %s: %s", path, msg)
    return docs


def summarize(doc: Document) -> dict[str, object]:
    """One line of facts about a parsed manual."""
    codes = doc.data.get("fault_codes") or []
    pages = [r["page"] for r in codes if r.get("page")]
    return {
        "file": doc.filename,
        "pages": doc.data.get("pages"),
        "chunks": len(doc.chunks),
        "tables": len(doc.tables),
        "fault_codes": len(codes),
        "fault_pages": f"{min(pages)}-{max(pages)}" if pages else "-",
        "warnings": len(doc.warnings),
    }
