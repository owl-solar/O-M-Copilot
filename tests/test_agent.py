import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from om_copilot import agent as agent_mod
from om_copilot.agent import BudgetStop, Spend, run_agent
from om_copilot.agent_eval import (
    LOG_REPORT,
    LOG_TOOLS,
    QA_REPLY,
    QA_TOOLS,
    answer_from_reply,
    check_log_run,
    oracle_chronic,
)
from om_copilot.mcp_server import Tools
from om_copilot.synth import CodeRef, generate, write_jsonl

DOC = SimpleNamespace(
    filename="Inverter Alarm Reference.pdf", sha256="ab" * 32, chunks=[], tables=[], warnings=[],
    data={"pages": 90, "fault_codes": [
        {"code": "2001", "name": "String Voltage High", "severity": "Major", "page": 12, "measures": ["1. Check."]},
        {"code": "450", "name": "Range fault", "page": 40, "measures": ["1. Look."]},
    ]},
)
POOL = [CodeRef("SUN2000", 2001, "Major"), CodeRef("SUN2000", 450, "unknown")]
USAGE = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeRetriever:
    def search(self, query, top_k=8, **filters):
        return []


def use(name, **kw):
    return SimpleNamespace(type="tool_use", name=name, input=kw, id=f"id-{name}")


class FakeClient:
    """Plays a list of replies; a forced tool_choice always gets the final tool."""

    def __init__(self, steps, final):
        self.steps, self.final, self.requests = list(steps), final, []
        self.messages = self

    def create(self, **req):
        self.requests.append(req)
        choice = req["tool_choice"]
        block = self.final if choice["type"] == "tool" else (self.steps.pop(0) if self.steps else self.final)
        return SimpleNamespace(content=[block], usage=USAGE, stop_reason="tool_use")


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(agent_mod, "guarded_create",
                        lambda client, *, purpose, file, sha256, kind, amount, allow_external=None, **req:
                        client.messages.create(**req))


@pytest.fixture
def tools(tmp_path):
    truth: list[dict] = []
    write_jsonl(generate(POOL, n=300, seed=7, truth=truth), tmp_path / "log.jsonl")
    t = Tools(docs=[DOC], retriever=FakeRetriever(), log_path=str(tmp_path / "log.jsonl"), orders_dir=str(tmp_path / "orders"))
    t.truth = truth
    return t


def go(tools, client, **kw):
    args = {"client": client, "spend": Spend(cap_usd=1.0), "system": "s", "user": "u", "allowed": QA_TOOLS, "final_name": "reply",
                "final_description": "d", "final_schema": QA_REPLY, "max_steps": 5}
    args.update(kw)
    return asyncio.run(run_agent(tools, **args))


def test_loop_calls_tools_over_mcp_and_returns_the_reply(tools):
    final = use("reply", answer="a", found=True, citations=[])
    client = FakeClient([use("lookup_fault_code", code=2001)], final)
    run = go(tools, client)
    assert run.rounds == 1 and not run.forced and run.result["found"] is True
    assert run.calls[0]["output"]["matches"][0]["source"]["page"] == 12
    assert run.usd > 0 and run.input_tokens == 2000 and run.seconds >= 0


def test_only_allowed_tools_are_offered(tools):
    client = FakeClient([], use("reply", answer="a", found=False, citations=[]))
    go(tools, client)
    offered = {t["name"] for t in client.requests[0]["tools"]}
    assert offered == QA_TOOLS | {"reply"}
    assert not offered & {"ask_manual", "propose_work_order", "get_incidents"}


def test_step_limit_forces_the_final_reply(tools):
    final = use("reply", answer="a", found=False, citations=[])
    client = FakeClient([use("search_manual", query="x") for _ in range(10)], final)
    run = go(tools, client, max_steps=2)
    assert run.rounds == 2 and run.forced and run.result["found"] is False
    assert len(client.requests) == 3 and client.requests[-1]["tool_choice"]["type"] == "tool"


def test_spending_cap_stops_the_run(tools):
    client = FakeClient([use("search_manual", query="x")] * 5, use("reply", answer="a", found=True, citations=[]))
    with pytest.raises(BudgetStop):
        go(tools, client, spend=Spend(cap_usd=0.000001))


def test_quotes_are_checked_against_the_page_text():
    pages = {("f.pdf", 3): "Check   the string layout first."}
    reply = {"answer": "x", "found": True, "citations": [
        {"file": "f.pdf", "page": 3, "quote": "check the string layout"}, {"file": "f.pdf", "page": 4, "quote": "check"}]}
    a, malformed = answer_from_reply("q", reply, pages, None)
    assert [c.verified for c in a.citations] == [True, False] and a.verified and not malformed
    a, malformed = answer_from_reply("q", {"answer": "x", "citations": []}, pages, None)  # 'found' is missing
    assert a.found is False and malformed
    assert answer_from_reply("q", None, pages, None)[0].found is False


def test_log_report_is_scored(tools):
    incidents, _ = tools._incidents()
    want = oracle_chronic(incidents)
    assert {(t["asset_id"], t["code"]) for t in tools.truth if t["kind"] == "chronic"} <= set(want)
    pairs = [{"asset_id": a, "fault_code": c, "repeat_incidents": n} for (a, c), n in want.items()]
    outputs = [{"n": n} for n in want.values()]
    good = check_log_run(tools, tools.truth, outputs, {"pairs": pairs, "drafts": [], "summary": "ok"})
    assert good["pairs_recall"] == f"{len(want)}/{len(want)}" and good["extra_pairs"] == 0
    assert good["failures"]["drafts"]  # no drafts reported and none saved: not the two most urgent
    bad = [{**pairs[0], "repeat_incidents": 99}, {"asset_id": "INV-99", "fault_code": 1, "repeat_incidents": 5}]
    res = check_log_run(tools, tools.truth, outputs, {"pairs": bad, "drafts": [], "summary": "99 and 5"})
    assert res["failures"]["counts"] and res["failures"]["pairs"] and res["ungrounded_numbers"] >= 1
    assert check_log_run(tools, tools.truth, [], None)["failures"]["pairs"] == ["no structured report"]


def test_task_tool_sets_are_consistent():
    assert "propose_work_order" in LOG_TOOLS and "propose_work_order" not in QA_TOOLS
    assert "approve" not in " ".join(LOG_TOOLS | QA_TOOLS) and LOG_REPORT["required"]


def test_quotes_are_checked_against_what_the_tools_returned():
    from om_copilot.agent_eval import tool_evidence

    calls = [
        {"name": "lookup_fault_code", "output": {"matches": [{"name": "Fan fault", "measures": ["1. Check the fan."],
                                                               "source": {"file": "f.pdf", "page": 5, "page_end": 6}}]}},
        {"name": "search_manual", "output": {"hits": [{"file": "g.pdf", "page": 9, "text": "Power off first."}]}},
        {"name": "get_incidents", "output": {"incidents": []}},
    ]
    ev = tool_evidence(calls)
    assert set(ev) == {("f.pdf", 5), ("f.pdf", 6), ("g.pdf", 9)}
    reply = {"answer": "a", "found": True, "citations": [
        {"file": "f.pdf", "page": 6, "quote": "1. Check the fan."}, {"file": "g.pdf", "page": 9, "quote": "power off"},
        {"file": "g.pdf", "page": 10, "quote": "power off"}]}
    a, _ = answer_from_reply("q", reply, ev, None)
    assert [c.verified for c in a.citations] == [True, True, False]


def test_a_model_that_stops_with_text_is_reminded_once(tools):
    text = SimpleNamespace(type="text", text="Here is my summary.")
    final = use("reply", answer="a", found=True, citations=[])
    client = FakeClient([text, final], final)
    run = go(tools, client)
    assert run.nudged and run.result["found"] is True and run.rounds == 0
    assert any(isinstance(m["content"], str) and "Call the `reply` tool" in m["content"]
               for m in client.requests[1]["messages"])
    client = FakeClient([text, text], final)  # text twice: no result, no endless loop
    run = go(tools, client)
    assert run.result is None and run.nudged and run.text == "Here is my summary."
