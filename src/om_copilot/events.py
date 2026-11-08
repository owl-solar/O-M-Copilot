"""Fault log in, incidents out.

``read_log`` takes a CSV (SCADA-style export, column names matched by aliases from the config) or our
JSONL. ``group`` turns raw events into incidents: the same code on the same inverter within
``window_hours`` is ONE incident (an alarm storm is not 40 work orders). An incident is a repeat when the
same code came back on the same inverter ``repeat_min`` or more times within ``repeat_days``.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

TIME_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y/%m/%d %H:%M:%S")
REQUIRED = ("ts", "asset_id", "model", "fault_code")


@dataclass
class LogReport:
    """What was read and what was skipped (never silently)."""
    events: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)  # our field -> column found in the file


def parse_time(value: str) -> str:
    """ISO text with seconds, or ValueError."""
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "")).isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    raise ValueError(f"unknown time format: {value!r}")


def _match_columns(header: list[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    lowered = {h.strip().lower(): h for h in header}
    found: dict[str, str] = {}
    for name, names in aliases.items():
        for alias in names:
            if alias.lower() in lowered:
                found[name] = lowered[alias.lower()]
                break
    return found


def read_csv(path: str | Path, default_model: str | None = None) -> LogReport:
    report = LogReport()
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        cols = _match_columns(reader.fieldnames or [], config.load()["log_columns"])
        report.columns = cols
        missing = [k for k in REQUIRED if k not in cols and not (k == "model" and default_model)]
        if missing:
            raise ValueError(
                f"columns not recognised for: {', '.join(missing)}. Found columns: {reader.fieldnames}. "
                "Add the names to 'log_columns' in data/config.local.json.")
        for i, row in enumerate(reader, start=2):
            try:
                event = {
                    "ts": parse_time(row[cols["ts"]]),
                    "asset_id": row[cols["asset_id"]].strip(),
                    "model": row[cols["model"]].strip() if "model" in cols else default_model,
                    "fault_code": int(float(row[cols["fault_code"]])),
                    "site": row[cols["site"]].strip() if "site" in cols else "unknown",
                }
                if not event["asset_id"] or not event["model"]:
                    raise ValueError("empty asset or model")
            except (ValueError, KeyError, TypeError) as e:
                report.skipped.append(f"line {i}: {e}")
                continue
            report.events.append(event)
    return report


def read_jsonl(path: str | Path) -> LogReport:
    report = LogReport()
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            report.events.append({"ts": parse_time(e["ts"]), "asset_id": e["asset_id"], "model": e["model"],
                                  "fault_code": int(e["fault_code"]), "site": e.get("site") or "unknown"})
        except (ValueError, KeyError, TypeError) as err:
            report.skipped.append(f"line {i}: {err}")
    return report


def read_log(path: str | Path, default_model: str | None = None) -> LogReport:
    report = read_jsonl(path) if str(path).lower().endswith((".jsonl", ".json")) else read_csv(path, default_model)
    report.events.sort(key=lambda e: (e["ts"], e["asset_id"]))
    return report


def group(events: list[dict[str, Any]], window_hours: float | None = None, repeat_days: float | None = None,
          repeat_min: int | None = None) -> list[dict[str, Any]]:
    """Incidents sorted by time. Keys: the event keys of the first event plus ``occurrences``, ``last_ts``,
    ``prior_incidents`` (earlier incidents of the same code on the same inverter within ``repeat_days``)
    and ``repeated`` (this incident is at least the ``repeat_min``-th in that period)."""
    rules = config.load()["grouping"]
    window = timedelta(hours=window_hours if window_hours is not None else rules["window_hours"])
    period = timedelta(days=repeat_days if repeat_days is not None else rules["repeat_days"])
    minimum = repeat_min if repeat_min is not None else rules["repeat_min"]

    by_key: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for e in sorted(events, key=lambda e: e["ts"]):
        by_key[(e["model"], e["asset_id"], e["fault_code"])].append(e)

    incidents: list[dict[str, Any]] = []
    for items in by_key.values():
        current: list[dict[str, Any]] = []
        mine: list[dict[str, Any]] = []
        for e in items:
            if current and datetime.fromisoformat(e["ts"]) - datetime.fromisoformat(current[-1]["ts"]) <= window:
                current.append(e)
                continue
            if current:
                mine.append(_incident(current))
            current = [e]
        mine.append(_incident(current))
        for inc in mine:
            start = datetime.fromisoformat(inc["ts"])
            inc["prior_incidents"] = sum(1 for o in mine if o is not inc and timedelta(0) < start - datetime.fromisoformat(o["ts"]) <= period)
            inc["repeated"] = inc["prior_incidents"] + 1 >= minimum
        incidents.extend(mine)
    return sorted(incidents, key=lambda i: (i["ts"], i["asset_id"]))


def summary(events: list[dict[str, Any]], incidents: list[dict[str, Any]]) -> dict[str, int]:
    """The numbers a person wants first: events in, incidents out, repeats, alarms saved."""
    return {"events": len(events), "incidents": len(incidents),
            "repeats": sum(1 for i in incidents if i["repeated"]),
            "merged_away": len(events) - len(incidents)}


def filter_incidents(incidents: list[dict[str, Any]], *, repeated_only: bool = False,
                     models: list[str] | None = None, sites: list[str] | None = None) -> list[dict[str, Any]]:
    return [i for i in incidents
            if (not repeated_only or i["repeated"])
            and (not models or i["model"] in models)
            and (not sites or i["site"] in sites)]


def _incident(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {**items[0], "occurrences": len(items), "last_ts": items[-1]["ts"]}
