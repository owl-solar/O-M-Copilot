from types import SimpleNamespace

import pytest
from design_ingest.llm import ExternalCallBlocked

from om_copilot.answer import answer, grade, summarize

FILE = "Manual.pdf"
HIT = {"file": FILE, "page": 131, "kind": "fault_code", "text": "Fault code 39: Low insulation. Check the ISO resistance."}


class FakeRetriever:
    def search(self, query, top_k=8, **filters):
        return [HIT] if "fault" in query else []


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.reply)], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=500, output_tokens=40))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_CACHE", str(tmp_path))
    monkeypatch.setenv("DESIGN_INGEST_LLM_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.delenv("DESIGN_INGEST_ALLOW_EXTERNAL", raising=False)


Q = {"id": "q1", "answerable": True, "expected_file": FILE, "expected_pages": [131], "must_contain": ["ISO resistance"]}
GOOD = {"answer": "Check the ISO resistance.", "found": True,
        "citations": [{"n": 1, "quote": "Check the ISO resistance."}]}


def test_blocked_without_permission():
    with pytest.raises(ExternalCallBlocked):
        answer("fault 39?", FakeRetriever(), client=FakeClient(GOOD))


def test_good_answer_passes_all_checks():
    a = answer("fault 39?", FakeRetriever(), client=FakeClient(GOOD), allow_external=True)
    assert grade(Q, a) == {"id": "q1", "ok": True, "found": True, "all_verified": True,
                           "cited_expected": True, "must_contain": True}


def test_invented_quote_or_wrong_page_fails():
    bad = {**GOOD, "citations": GOOD["citations"] + [{"n": 1, "quote": "never written in the manual"}]}
    a = answer("fault 39?", FakeRetriever(), client=FakeClient(bad), allow_external=True)
    assert not grade(Q, a)["ok"] and not grade(Q, a)["all_verified"]
    a = answer("fault 39?", FakeRetriever(), client=FakeClient(GOOD), allow_external=True)
    assert not grade({**Q, "expected_pages": [5]}, a)["cited_expected"]


def test_unanswerable_must_be_refused_and_no_hits_means_no_call():
    unans = {"id": "q9", "answerable": False}
    fake = FakeClient({})
    a = answer("battery warranty?", FakeRetriever(), client=fake, allow_external=True)  # no hits
    assert fake.calls == [] and grade(unans, a)["ok"]
    claims = FakeClient(GOOD)
    a = answer("fault 39?", FakeRetriever(), client=claims, allow_external=True)
    assert not grade(unans, a)["ok"]


def test_summarize_shares():
    g = [{"id": "a", "ok": True, "found": True, "all_verified": True, "cited_expected": True, "must_contain": False},
         {"id": "b", "ok": False, "found": True, "all_verified": False, "cited_expected": True, "must_contain": True},
         {"id": "c", "ok": True, "refused": True}]
    s = summarize(g)
    assert s["all_citations_verified"] == 0.5 and s["cited_expected_page"] == 1.0 and s["refused_unanswerable"] == 1.0
