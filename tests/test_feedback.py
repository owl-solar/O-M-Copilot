import pytest

from om_copilot import feedback as fb
from om_copilot.workorder import Item, Source, WorkOrder


def order(oid="a", code=7, n_steps=2, model="M"):
    steps = [Item(n=i, text=f"step {i}", source=Source(file="M.pdf", page=10 + i)) for i in range(1, n_steps + 1)]
    return WorkOrder(id=oid, ts="2026-01-01T00:00:00", asset_id="INV-01", site="S", model=model, fault_code=code,
                     found=True, steps=steps)


def test_record_appends_and_checks_the_step(tmp_path):
    path = tmp_path / "f.jsonl"
    rec = fb.record(order(), 2, "helped", note="ok", who="anna", path=path)
    assert rec["page"] == 12 and rec["verdict"] == "helped"
    fb.record(order(), 1, "failed", path=path)
    assert [r["step_n"] for r in fb.read(path)] == [2, 1]
    with pytest.raises(ValueError, match="no step 3"):
        fb.record(order(), 3, "helped", path=path)
    with pytest.raises(ValueError, match="verdict"):
        fb.record(order(), 1, "maybe", path=path)


def test_last_verdict_per_step_counts_and_rate_is_overall_and_per_code(tmp_path):
    path = tmp_path / "f.jsonl"
    fb.record(order("a"), 1, "failed", path=path)
    fb.record(order("a"), 1, "helped", path=path)  # changed mind: only this one counts
    fb.record(order("a"), 2, "helped", path=path)
    for oid in ("b", "c", "d"):  # code 9: three failures -> flagged for review
        fb.record(order(oid, code=9), 1, "failed", path=path)
    s = fb.summarize(fb.read(path))
    assert (s["verdicts"], s["helped"], s["failed"]) == (5, 2, 3)
    assert s["codes"][0]["fault_code"] == 9 and s["codes"][0]["review"] and s["codes"][0]["rate"] == 0
    assert [r["fault_code"] for r in s["to_review"]] == [9]
    assert next(r for r in s["codes"] if r["fault_code"] == 7)["rate"] == 1.0


def test_empty_log_and_few_verdicts_are_not_flagged(tmp_path):
    assert fb.read(tmp_path / "none.jsonl") == [] and fb.summarize([])["rate"] is None
    path = tmp_path / "f.jsonl"
    fb.record(order(), 1, "failed", path=path)  # one verdict is too little to flag
    assert fb.summarize(fb.read(path))["to_review"] == []


def test_synthetic_verdicts_are_marked_and_reproducible():
    orders = [order(str(i), code=i) for i in range(10)]
    a, b = fb.synthetic(orders, seed=1), fb.synthetic(orders, seed=1)
    assert a == b and a and all(r["synthetic"] and r["who"] == "synthetic" for r in a)
