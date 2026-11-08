import json
from types import SimpleNamespace

import pytest

from om_copilot.agent_eval import passed, run_checks, scripted_agent, ungrounded_numbers
from om_copilot.mcp_server import Tools
from om_copilot.orders import approve
from om_copilot.synth import CodeRef, generate, write_jsonl

DOC = SimpleNamespace(
    filename="Inverter Alarm Reference.pdf", sha256="ab" * 32, chunks=[], tables=[], warnings=[],
    data={"pages": 90, "fault_codes": [
        {"code": "2001", "name": "String Voltage High", "severity": "Major", "page": 12, "measures": ["1. Check."]},
        {"code": "450", "name": "Range fault", "page": 40, "measures": ["1. Look."]},
    ]},
)
POOL = [CodeRef("SUN2000", 2001, "Major"), CodeRef("SUN2000", 450, "unknown")]


class FakeRetriever:
    def search(self, query, top_k=8, **filters):
        return []


@pytest.fixture
def setup(tmp_path):
    truth: list[dict] = []
    events = generate(POOL, n=300, seed=7, truth=truth)
    write_jsonl(events, tmp_path / "log.jsonl")
    tools = Tools(docs=[DOC], retriever=FakeRetriever(), log_path=str(tmp_path / "log.jsonl"),
                  orders_dir=str(tmp_path / "orders"))
    return tools, truth


def test_generator_truth_does_not_change_the_log():
    truth: list[dict] = []
    assert generate(POOL, n=300, seed=7, truth=truth) == generate(POOL, n=300, seed=7)
    assert {t["kind"] for t in truth} == {"cluster", "storm", "chronic"}
    assert all(t["events"] >= 1 for t in truth)


def test_scripted_agent_passes_all_checks(setup):
    tools, truth = setup
    res = run_checks(tools, truth, POOL, scripted_agent(tools, truth))
    assert passed(res), res["failures"]
    assert res["writes"].endswith("0 without confirmation") and res["ungrounded_numbers"] == 0


def test_a_wrong_priority_is_caught(setup):
    tools, truth = setup
    run = scripted_agent(tools, truth)
    next(iter(run["rows"].values()))["priority"] = "P9"
    assert run_checks(tools, truth, POOL, run)["failures"]["priority"]


def test_a_missed_storm_or_repeat_is_caught(setup):
    tools, truth = setup
    run = scripted_agent(tools, truth)
    run["rows"] = {}
    failures = run_checks(tools, truth, POOL, run)["failures"]
    assert failures["storms"] and failures["chronic"]


def test_an_approved_order_counts_as_a_write_without_confirmation(setup, tmp_path):
    tools, truth = setup
    run = scripted_agent(tools, truth)
    approve(run["drafted"][0], "Somebody", tmp_path / "orders")
    res = run_checks(tools, truth, POOL, run)
    assert res["writes"].startswith("3 drafts, 1 ") and res["failures"]["writes"]


def test_an_extra_file_is_caught(setup, tmp_path):
    tools, truth = setup
    run = scripted_agent(tools, truth)
    path = tmp_path / "orders" / run["drafted"][0]
    (tmp_path / "orders" / "other.json").write_text(
        (tmp_path / "orders" / f"{path.name}.json").read_text(encoding="utf-8").replace(path.name, "other"),
        encoding="utf-8")
    assert run_checks(tools, truth, POOL, run)["failures"]["writes"]


def test_numbers_must_come_from_tool_output():
    outputs = [{"summary": {"events": 300, "incidents": 241}, "ts": "2026-01-02T00:08:50"}]
    assert ungrounded_numbers("300 events, 241 incidents on 2026-01-02", outputs) == []
    assert ungrounded_numbers("300 events, 250 incidents", outputs) == ["250"]
    assert json.dumps(outputs)  # outputs stay plain JSON
