from dataclasses import asdict

import pytest

from om_copilot import config
from om_copilot.events import group, parse_time, read_log
from om_copilot.synth import CodeRef, generate, write_csv
from om_copilot.workorder import WorkOrder, to_markdown


def ev(ts, asset="INV-01", code=10, model="M"):
    return {"ts": ts, "asset_id": asset, "model": model, "fault_code": code, "site": "S"}


def test_alarm_storm_is_one_incident_and_far_apart_events_are_not():
    events = [ev("2026-03-01T10:00:00"), ev("2026-03-01T10:05:00"), ev("2026-03-01T13:00:00"),
              ev("2026-03-02T10:00:00"), ev("2026-03-01T10:00:00", asset="INV-02")]
    inc = group(events, window_hours=6)
    inv1 = [i for i in inc if i["asset_id"] == "INV-01"]
    assert [i["occurrences"] for i in inv1] == [3, 1] and inv1[0]["last_ts"] == "2026-03-01T13:00:00"
    assert len(inc) == 3  # INV-02 is its own incident


def test_repeat_needs_enough_incidents_inside_the_period():
    days = ["2026-03-01", "2026-03-10", "2026-03-20", "2026-06-01"]
    inc = group([ev(f"{d}T08:00:00") for d in days], window_hours=6, repeat_days=30, repeat_min=3)
    assert [i["repeated"] for i in inc] == [False, False, True, False]
    assert [i["prior_incidents"] for i in inc] == [0, 1, 2, 0]


def test_different_code_or_inverter_does_not_count_as_repeat():
    events = [ev("2026-03-01T08:00:00"), ev("2026-03-05T08:00:00", code=11), ev("2026-03-09T08:00:00", asset="INV-02")]
    assert not any(i["repeated"] for i in group(events))


def test_times_in_common_formats():
    assert parse_time("02.01.2026 07:49:50") == "2026-01-02T07:49:50"
    assert parse_time("2026-01-02 07:49") == "2026-01-02T07:49:00"
    assert parse_time("01/02/2026 07:49") == "2026-01-02T07:49:00"
    with pytest.raises(ValueError):
        parse_time("yesterday")


def test_csv_with_scada_column_names_roundtrip_and_bad_rows_are_reported(tmp_path):
    pool = [CodeRef("M", 10, "Major"), CodeRef("M", 11, "Minor")]
    events = generate(pool, n=50, seed=1)
    path = tmp_path / "log.csv"
    write_csv(events, path)
    with path.open("a", encoding="utf-8") as f:
        f.write("not a time;S;INV-01;M;10\n")
    rep = read_log(path)
    assert len(rep.events) == 50 and len(rep.skipped) == 1 and "line 52" in rep.skipped[0]
    assert rep.columns["ts"] == "Timestamp" and {e["fault_code"] for e in rep.events} <= {10, 11}


def test_unknown_columns_give_a_helpful_error_and_default_model_fills_the_gap(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("When,Unit,No\n2026-01-01 10:00,INV-1,5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="log_columns"):
        read_log(path)
    path.write_text("Time,Device,Code\n2026-01-01 10:00,INV-1,5\n", encoding="utf-8")
    assert read_log(path, default_model="ABC").events[0]["model"] == "ABC"
    with pytest.raises(ValueError, match="model"):
        read_log(path)


def test_synth_has_storms_and_chronic_repeats():
    pool = [CodeRef("M", c, "Major") for c in range(1, 40)]
    inc = group([asdict(e) for e in generate(pool, n=600, seed=3)])
    assert len(inc) < 600 and any(i["occurrences"] > 1 for i in inc) and any(i["repeated"] for i in inc)


def test_repeat_raises_priority_one_level_and_shows_in_markdown():
    assert [config.escalate(p) for p in ("P3", "P2", "P1", "high")] == ["P2", "P1", "P1", "high"]
    wo = WorkOrder(id="x", ts="t", asset_id="INV-01", site="S", model="M", fault_code=1, found=True, occurrences=4,
                   last_ts="t2", repeated=True, prior_incidents=2)
    md = to_markdown(wo)
    assert "Alarms merged: 4" in md and "REPEAT" in md
