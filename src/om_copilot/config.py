"""Which manual belongs to which model, and the severity -> priority table.

Defaults ship with the package (``config_default.json``). Your own manuals go into a local,
git-ignored file (``data/config.local.json``, env OM_COPILOT_LOCAL_CONFIG), written by
``om-copilot add-manual`` or edited by hand::

    {"models": [{"match": "my file name part", "model": "ABC-1000"}], "priority": {"severe": "P1"}}

``match`` is a lower-case part of the manual's file name; the first match wins, local entries first.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_FILE = Path(__file__).with_name("config_default.json")
LOCAL_FILE = "data/config.local.json"


def local_path() -> Path:
    return Path(os.environ.get("OM_COPILOT_LOCAL_CONFIG", LOCAL_FILE))


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@lru_cache(maxsize=8)
def _merged(local: str) -> dict[str, Any]:
    default, mine = _read(DEFAULT_FILE), _read(Path(local))
    return {
        "models": mine.get("models", []) + default.get("models", []),
        "priority": {**default.get("priority", {}), **mine.get("priority", {})},
        "default_priority": mine.get("default_priority", default.get("default_priority", "P3")),
    }


def load() -> dict[str, Any]:
    return _merged(str(local_path()))


def model_of(filename: str) -> str:
    """Model label of a manual: first config entry whose ``match`` is in the file name, else the file stem."""
    name = filename.lower()
    for entry in load()["models"]:
        if entry["match"].lower() in name:
            return entry["model"]
    return Path(filename).stem


def priority_of(severity: str | None) -> str:
    cfg = load()
    return cfg["priority"].get((severity or "").strip().lower(), cfg["default_priority"])


def register_model(match: str, model: str) -> Path:
    """Write ``match -> model`` into the local config (replaces an entry with the same match)."""
    path = local_path()
    mine = _read(path)
    models = [m for m in mine.get("models", []) if m["match"].lower() != match.lower()]
    mine["models"] = [{"match": match.lower(), "model": model}] + models
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mine, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    _merged.cache_clear()
    return path
