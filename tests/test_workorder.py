from types import SimpleNamespace

from om_copilot.workorder import build, priority_of, to_markdown

DOC = SimpleNamespace(
    filename="Inverter Alarm Reference.pdf",
    data={"fault_codes": [
        {"code": "2001", "name": "String Voltage High", "severity": "Major", "page": 12, "page_end": 15,
         "measures": ["1. Check the string layout.", "2. Measure the open-circuit voltage."]},
        {"code": "448-479", "code_ranges": [[448, 479]], "name": "Range fault", "page": 40, "measures": []},
    ]},
)
EVENT = {"ts": "2026-01-02T00:00:00", "asset_id": "INV-15", "site": "Site D", "model": "SUN2000", "fault_code": 2001}


class FakeRetriever:
    def search(self, query, top_k=8, **filters):
        assert filters == {"files": [DOC.filename]}
        return [
            {"file": DOC.filename, "page": 12, "kind": "fault_code", "text": "Fault code 2001 ... DANGER"},
            {"file": DOC.filename, "page": 3, "kind": "text", "text": "Some intro without a label."},
            {"file": DOC.filename, "page": 5, "kind": "table", "text": "| DANGER |\n|---|\n| Power off first. |"},
            {"file": DOC.filename, "page": 6, "kind": "table", "text": "| DANGER |\n|---|\n| Power off first. |"},
        ]


def test_steps_are_the_manual_measures_with_pages():
    wo = build([DOC], EVENT, FakeRetriever())
    assert wo.found and wo.fault_name == "String Voltage High" and wo.priority == "P2"
    assert [s.text for s in wo.steps] == ["Check the string layout.", "Measure the open-circuit voltage."]
    assert all(s.source.file == DOC.filename and s.source.page == 12 and s.source.page_end == 15 for s in wo.steps)


def test_safety_only_labelled_passages_without_repeats():
    wo = build([DOC], EVENT, FakeRetriever())
    assert [(s.text, s.source.page) for s in wo.safety] == [("DANGER Power off first.", 5)]


def test_unknown_code_or_model_gives_no_steps_and_says_so():
    for patch in ({"fault_code": 9999}, {"model": "Other"}):
        wo = build([DOC], {**EVENT, **patch})
        assert not wo.found and wo.steps == [] and "not found" in wo.note


def test_range_code_without_measures_is_noted():
    wo = build([DOC], {**EVENT, "fault_code": 450})
    assert wo.found and wo.steps == [] and "no corrective measures" in wo.note


def test_priority_table_and_markdown():
    assert priority_of("Major") == "P2" and priority_of(None) == "P3" and priority_of("Critical") == "P1"
    md = to_markdown(build([DOC], EVENT, FakeRetriever()))
    assert md.startswith("# Work order (DRAFT): INV-15, fault 2001")
    assert "1. Check the string layout. [Inverter Alarm Reference.pdf, pp.12-15]" in md
    assert "## Safety" in md
