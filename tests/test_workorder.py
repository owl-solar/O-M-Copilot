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


def test_priority_explains_itself_and_sla_follows_final_priority(monkeypatch, tmp_path):
    local = tmp_path / "c.json"
    local.write_text('{"rated_kw": {"INV-15": 2000}, "sla_hours": {"P1": 2}}', encoding="utf-8")
    monkeypatch.setenv("OM_COPILOT_LOCAL_CONFIG", str(local))
    plain = build([DOC], {**EVENT, "asset_id": "INV-99"})  # SUN2000 default 100 kW: below the limit
    assert plain.priority == "P2" and plain.rated_kw == 100 and plain.respond_by == "2026-01-03T00:00"
    big = build([DOC], {**EVENT, "repeated": True, "occurrences": 3, "prior_incidents": 2})
    assert big.priority == "P1" and big.rated_kw == 2000 and big.respond_by == "2026-01-02T02:00"
    assert [r.split(" -> ")[1] for r in big.priority_reasons] == ["P2", "P1", "P1"]
    assert "rated 2000 kW" in to_markdown(big) and "Respond by: 2026-01-02T02:00" in to_markdown(big)


def test_export_row_has_audit_fields_and_people_fields_stay_empty(tmp_path):
    import csv

    from om_copilot.export import COLUMNS, load_orders, write_csv

    doc = SimpleNamespace(filename=DOC.filename, data=DOC.data, sha256="abc123")
    wo = build([doc], EVENT)
    assert wo.id == "2026-01-02T000000_INV-15_2001" and wo.manual_sha256 == "abc123" and wo.status == "draft"
    (tmp_path / f"{wo.id}.json").write_text(wo.model_dump_json(), encoding="utf-8")
    (tmp_path / "old.json").write_text('{"ts": "x"}', encoding="utf-8")
    orders, skipped = load_orders(tmp_path)
    assert skipped == ["old.json"]
    assert write_csv(orders, tmp_path / "o.csv", delimiter=";") == 1
    with (tmp_path / "o.csv").open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f, delimiter=";"))
    assert list(row) == list(COLUMNS)
    assert row["manual_sha256"] == "abc123" and row["manual_pages"] == "12,15" and row["priority"] == "P2"
    assert "1. Check the string layout. [p.12-15]" in row["steps"]
    assert row["assignee"] == row["approved_by"] == row["work_done"] == "" and row["repeat"] == "no"


def test_csv_text_equals_the_file_version(tmp_path):
    from om_copilot.export import to_csv_text, write_csv

    wo = build([DOC], EVENT)
    write_csv([wo], tmp_path / "o.csv")
    with (tmp_path / "o.csv").open(encoding="utf-8-sig", newline="") as f:
        assert f.read() == to_csv_text([wo])
