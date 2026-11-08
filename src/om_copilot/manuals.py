"""Check a manual and add it to the corpus: the user-facing side of "bring your own manual"."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from design_ingest import Document, ingest

from . import config
from .corpus import raw_dir, summarize

NO_CODES_HINT = (
    "No fault-code table was recognised. Questions still work through text search; the exact code lookup\n"
    "and work orders for this manual do not. A table is recognised by headings like Fault Code / Fault Name /\n"
    "Corrective Measures (also Error, Alarm, Remedy, Action) and ruled rows. If your manual has such a table,\n"
    "the reader in design-ingest needs to learn its layout (see README, 'If no fault codes were found')."
)


def inspect(doc: Document) -> dict[str, Any]:
    """Facts about one manual and what still needs the user's attention."""
    info = dict(summarize(doc))
    info["model"] = config.model_of(doc.filename)
    severities = {str(r["severity"]).strip().lower() for r in doc.data.get("fault_codes") or [] if r.get("severity")}
    info["unknown_severities"] = sorted(s for s in severities if s not in config.load()["priority"])
    notes: list[str] = []
    if not info["fault_codes"]:
        notes.append(NO_CODES_HINT)
    if info["unknown_severities"]:
        notes.append("Severity words not in the priority table (they get the default priority): "
                     + ", ".join(info["unknown_severities"]) + ". Add them under \"priority\" in the local config.")
    if info["model"] == Path(doc.filename).stem:
        notes.append("No model label is set for this file, the file name is used. "
                     "Set one: om-copilot add-manual <file> --model LABEL")
    info["notes"] = notes
    return info


def format_report(info: dict[str, Any]) -> str:
    counts = (f"  {info['pages']} pages, {info['chunks']} chunks, {info['tables']} tables, "
              f"{info['fault_codes']} fault codes (pp. {info['fault_pages']}), {info['warnings']} warnings")
    lines = [f"{info['file']}", f"  model label: {info['model']}", counts]
    lines += [f"  ! {n}" for n in info["notes"]]
    return "\n".join(lines)


def add_manual(path: str | Path, model: str | None = None) -> tuple[Document, dict[str, Any], Path]:
    """Copy the PDF into the manuals folder (if it is not there), read it, store its model label.

    Returns the parsed document, the report and the folder entry. Raises FileNotFoundError / ValueError
    for a missing file or a file design-ingest cannot read."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"{src} not found")
    folder = raw_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    if not dest.exists() or not dest.samefile(src):
        shutil.copy2(src, dest)
    doc = ingest(dest)
    label = model or config.model_of(dest.name)
    config.register_model(dest.stem, label)
    return doc, inspect(doc), dest
