import json

from om_copilot.evaluation import distinct_pages, evaluate, load_questions, rank_of_expected, score


class FakeIndex:
    def __init__(self, hits_by_query):
        self.hits_by_query = hits_by_query

    def search(self, query, top_k=10):
        return self.hits_by_query[query][:top_k]


def h(file, page):
    return {"file": file, "page": page}


def test_distinct_pages_keeps_first_rank():
    ranked = distinct_pages([h("a", 1), h("a", 1), h("a", 2), h("b", 1)])
    assert ranked == [("a", 1), ("a", 2), ("b", 1)]


def test_rank_of_expected_matches_file_and_page():
    ranked = [("a", 5), ("b", 7), ("a", 7)]
    assert rank_of_expected(ranked, "a", [7, 8]) == 3
    assert rank_of_expected(ranked, "c", [7]) is None


def test_score_hit_and_mrr():
    s = score([1, 2, None, 6])
    assert s["n"] == 4
    assert s["hit@1"] == 0.25 and s["hit@3"] == 0.5 and s["hit@5"] == 0.5
    assert abs(s["mrr"] - (1 + 0.5 + 1 / 6) / 4) < 1e-9


def test_evaluate_skips_unanswerable_and_lists_misses():
    qs = [
        {"id": "q1", "type": "fault_code", "question": "a", "expected_file": "f", "expected_pages": [3], "answerable": True},
        {"id": "q2", "type": "fault_code", "question": "b", "expected_file": "f", "expected_pages": [9], "answerable": True},
        {"id": "q3", "type": "unanswerable", "question": "c", "expected_file": None, "expected_pages": [], "answerable": False},
    ]
    idx = FakeIndex({"a": [h("f", 3)], "b": [h("f", 1), h("f", 2)]})
    res = evaluate(idx, qs)
    assert res["overall"]["n"] == 2 and res["overall"]["hit@1"] == 0.5
    assert res["misses"] == ["q2"] and res["ranks"] == {"q1": 1, "q2": None}


def test_question_file_is_consistent():
    from pathlib import Path

    qs = load_questions(Path(__file__).parent.parent / "eval" / "questions.json")
    assert len(qs) == 25 and len({q["id"] for q in qs}) == 25
    for q in qs:
        assert q["answerable"] == bool(q["expected_file"]) == bool(q["expected_pages"])
    assert json.dumps(qs)  # serialisable
