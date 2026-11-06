from design_ingest import Document

from om_copilot.retrieval import Retriever, build_code_table, codes_in_question, find_codes


def doc(name, records) -> Document:
    return Document(
        source="x", filename=name, file_type="pdf", sha256="0" * 64, size_bytes=1,
        parser="pdf", parser_version="1", data={"fault_codes": records},
    )


SUNGROW = doc("sg.pdf", [
    {"code": "4, 5", "code_ranges": [[4, 4], [5, 5]], "name": "Grid Undervoltage", "measures": ["Check grid."], "page": 127},
    {"code": "28, 448-479", "code_ranges": [[28, 28], [448, 479]], "name": "PV Fault", "measures": ["Check PV."], "page": 129},
])
HUAWEI = doc("hw.pdf", [{"code": "2062", "code_ranges": None, "name": "Low Insulation Resistance", "measures": ["Check PE."], "page": 52}])
SMA = doc("sma.pdf", [
    {"code": "0103", "code_ranges": [[103, 103]], "name": "Grid voltage too high", "measures": ["Check voltage."], "page": 69},
    {"code": "*Depending", "code_ranges": None, "name": "footnote", "measures": [], "page": 70},
])


def test_table_handles_ranges_plain_codes_leading_zero_and_footnote():
    t = build_code_table([SUNGROW, HUAWEI, SMA])
    assert {(e.file, e.lo, e.hi) for e in t} == {
        ("sg.pdf", 4, 4), ("sg.pdf", 5, 5), ("sg.pdf", 28, 28), ("sg.pdf", 448, 479),
        ("hw.pdf", 2062, 2062), ("sma.pdf", 103, 103),
    }  # '*Depending' is not a numeric code


def test_codes_need_a_cue_word():
    assert codes_in_question("SG350HX shows fault 39") == [39]
    assert codes_in_question("What does fault code 450 mean?") == [450]
    assert codes_in_question("Huawei alarm 2062 and error 3504") == [2062, 3504]
    assert codes_in_question("SMA event 4003 reverse currents") == [4003]
    assert codes_in_question("wait 25 minutes on the SG350HX at 0.01 A, error E306") == []


def test_find_code_inside_range_and_in_each_manual():
    t = build_code_table([SUNGROW, HUAWEI, SMA])
    assert [(e.file, e.page) for e in find_codes("fault code 450", t)] == [("sg.pdf", 129)]
    assert [(e.file, e.page) for e in find_codes("fault 5", t)] == [("sg.pdf", 127)]
    assert [(e.file, e.page) for e in find_codes("error 103", t)] == [("sma.pdf", 69)]
    assert find_codes("fault 999", t) == []


class FakeIndex:
    def search(self, query, top_k=8, **_):
        return [
            {"file": "sg.pdf", "page": 1, "text": "other", "score": 3.0},
            {"file": "sg.pdf", "page": 127, "text": "Fault code 4, 5: Grid Undervoltage. Corrective measures: Check grid.", "score": 2.0},
        ][:top_k]


def test_retriever_puts_exact_code_first_and_keeps_the_rest(monkeypatch):
    r = Retriever.__new__(Retriever)
    r.index, r.table = FakeIndex(), build_code_table([SUNGROW])
    hits = r.search("fault 5", top_k=3)
    assert hits[0]["source"] == "code_lookup" and hits[0]["page"] == 127
    assert [h["page"] for h in hits] == [127, 1]  # duplicate of the exact record is not repeated
    assert [h["page"] for h in r.search("no code here", top_k=3)] == [1, 127]
