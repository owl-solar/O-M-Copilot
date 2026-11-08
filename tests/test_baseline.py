from types import SimpleNamespace

import pytest

from om_copilot.answer import grade
from om_copilot.baseline import ask_baseline, build_context, cost_usd

DOC = SimpleNamespace(filename="M.pdf", chunks=[
    SimpleNamespace(page=2, text="Second page. Wait 15 minutes."),
    SimpleNamespace(page=1, text="First page."),
    SimpleNamespace(page=1, text="More of page one."),
    SimpleNamespace(page=None, text="no page"),
])


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=20, output_tokens=40, cache_creation_input_tokens=0,
                                cache_read_input_tokens=1000)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.reply)],
                               stop_reason="tool_use", usage=usage)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_INGEST_LLM_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.delenv("DESIGN_INGEST_TOKEN_BUDGET", raising=False)


def test_context_is_page_ordered_and_keeps_text_per_page():
    ctx, pages = build_context([DOC])
    assert ctx.index("PAGE 1 ===") < ctx.index("PAGE 2 ===") and "no page" not in ctx
    assert pages[("M.pdf", 1)] == "First page.\nMore of page one."


def test_quotes_are_checked_against_the_page_they_name_and_graded_like_ours():
    ctx, pages = build_context([DOC])
    reply = {"answer": "15 minutes.", "found": True, "citations": [
        {"file": "M.pdf", "page": 2, "quote": "Wait 15 minutes."},   # right page
        {"file": "M.pdf", "page": 1, "quote": "Wait 15 minutes."}]}  # right words, wrong page
    answer, usage = ask_baseline("how long?", ctx, pages, client=FakeClient(reply), allow_external=True)
    assert [c.verified for c in answer.citations] == [True, False]
    q = {"id": "q", "answerable": True, "expected_file": "M.pdf", "expected_pages": [2],
         "must_contain": ["15 minutes"]}
    g = grade(q, answer)
    assert g["cited_expected"] and not g["all_verified"] and not g["ok"]
    assert usage.cache_read_input_tokens == 1000


def test_manuals_go_in_one_cached_block_and_cost_estimate_uses_cache_prices():
    ctx, pages = build_context([DOC])
    fake = FakeClient({"answer": "x", "found": False, "citations": []})
    ask_baseline("q", ctx, pages, client=fake, allow_external=True)
    system = fake.calls[0]["system"]
    assert system[1]["cache_control"] == {"type": "ephemeral"} and system[1]["text"] == ctx
    u = SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                        cache_read_input_tokens=1_000_000)
    assert abs(cost_usd(u) - 0.05) < 1e-9
