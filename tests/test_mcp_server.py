import asyncio
import json
from types import SimpleNamespace

import pytest

from om_copilot.mcp_server import Tools

SHA = "ab" * 32
DOC = SimpleNamespace(
    filename="Inverter Alarm Reference.pdf",
    sha256=SHA, chunks=[], tables=[], warnings=[],
    data={"pages": 90, "fault_codes": [
        {"code": "2001", "name": "String Voltage High", "severity": "Major", "page": 12, "page_end": 15,
         "measures": ["1. Check the string layout.", "2. Measure the open-circuit voltage."]},
        {"code": "448-479", "code_ranges": [[448, 479]], "name": "Range fault", "page": 40, "measures": []},
    ]},
)


class FakeRetriever:
    def search(self, query, top_k=8, **filters):
        return [{"file": DOC.filename, "page": 12, "kind": "fault_code", "text": "x" * 2000}]


def _log(tmp_path, rows):
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _event(ts, code=2001, asset="INV-15"):
    return {"ts": ts, "asset_id": asset, "model": "SUN2000", "fault_code": code, "site": "Site D"}


@pytest.fixture
def tools(tmp_path):
    # a storm (3 alarms within minutes) and one alarm of another code
    rows = [_event("2026-01-02T00:00:00"), _event("2026-01-02T00:10:00"), _event("2026-01-02T00:20:00"),
            _event("2026-01-05T08:00:00", code=450, asset="INV-02")]
    return Tools(docs=[DOC], retriever=FakeRetriever(), log_path=_log(tmp_path, rows),
                 orders_dir=str(tmp_path / "orders"))


def test_lookup_exact_and_range(tools):
    r = tools.lookup_fault_code(2001)
    m = r["matches"][0]
    assert m["name"] == "String Voltage High" and len(m["measures"]) == 2
    assert m["source"] == {"file": DOC.filename, "page": 12, "page_end": 15, "sha256": SHA}
    assert tools.lookup_fault_code(460)["matches"][0]["source"]["page"] == 40


def test_lookup_unknown_code_or_model_says_so(tools):
    assert tools.lookup_fault_code(9999)["matches"] == []
    assert "not found" in tools.lookup_fault_code(9999)["note"]
    assert tools.lookup_fault_code(2001, model="Other")["matches"] == []


def test_search_shortens_text_and_adds_source(tools):
    hit = tools.search_manual("string voltage")["hits"][0]
    assert len(hit["text"]) <= 700 and hit["page"] == 12 and hit["sha256"] == SHA


def test_incidents_merge_storm_and_give_priority(tools):
    r = tools.get_incidents()
    assert r["summary"] == {"events": 4, "incidents": 2, "repeats": 0}
    first = r["incidents"][0]
    assert first["occurrences"] == 3 and first["priority"] == "P2" and first["respond_by"]
    assert tools.get_incidents(asset_id="INV-02")["matching"] == 1
    assert tools.get_incidents(limit=1)["shown"] == 1


def test_work_order_built_from_incident_then_read_from_disk(tools, tmp_path):
    oid = tools.get_incidents()["incidents"][0]["id"]
    r = tools.get_work_order(oid)
    assert r["saved"] is False and r["work_order"]["steps"][0]["source"]["page"] == 12
    assert r["work_order"]["manual_sha256"] == SHA
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / f"{oid}.json").write_text(json.dumps(r["work_order"]), encoding="utf-8")
    assert tools.get_work_order(oid)["saved"] is True


def test_work_order_id_cannot_leave_the_folder(tools):
    assert "Invalid" in tools.get_work_order("../secret")["note"]
    assert "No work order" in tools.get_work_order("nothing")["note"]


def test_ask_is_blocked_without_permission(tools, monkeypatch):
    monkeypatch.delenv("DESIGN_INGEST_ALLOW_EXTERNAL", raising=False)
    r = tools.ask_manual("what is fault 2001?")
    assert r["blocked"] is True and "answer" not in r


def test_server_registers_five_read_only_tools(tools):
    pytest.importorskip("mcp")
    from om_copilot.mcp_server import build_server

    server = build_server(tools)
    listed = asyncio.run(server.list_tools())
    assert {t.name for t in listed} == {"list_manuals", "lookup_fault_code", "search_manual", "ask_manual", "get_incidents",
                                        "get_work_order"}
    result = asyncio.run(server.call_tool("lookup_fault_code", {"code": 2001}))
    assert "String Voltage High" in str(result)


# ---- M2: the reader is visible ------------------------------------------------------------------------

def test_manifest_matches_om_copilot_manuals(tools, monkeypatch, capsys):
    from om_copilot import cli
    from om_copilot.manuals import inspect

    DOC.data["pages"] = 90
    monkeypatch.setattr(cli, "load_documents", lambda folder=None: [DOC])
    m = tools.list_manuals()
    entry = m["manuals"][0]
    info = inspect(DOC)
    assert (entry["file"], entry["pages"], entry["fault_codes"], entry["model"]) == (
        info["file"], info["pages"], info["fault_codes"], info["model"])
    assert entry["sha256"] == SHA and m["design_ingest_version"]

    cli.main(["manuals", "--json"])
    assert json.loads(capsys.readouterr().out) == m  # CLI and MCP print the same manifest


def test_every_answer_has_a_source_with_sha256(tools, monkeypatch, tmp_path):
    from om_copilot import mcp_server

    sources = [m["source"] for m in tools.lookup_fault_code(2001)["matches"]]
    sources += tools.search_manual("string voltage")["hits"]
    cite = SimpleNamespace(file=DOC.filename, page=12, verified=True, quote="q")
    monkeypatch.setattr(mcp_server, "answer", lambda q, r: SimpleNamespace(
        answer="a", found=True, verified=True, citations=[cite]))
    sources += tools.ask_manual("q")["citations"]
    oid = tools.get_incidents()["incidents"][0]["id"]
    wo = tools.get_work_order(oid)["work_order"]
    assert all(s["source"]["file"] == DOC.filename for s in wo["steps"])  # the sha256 sits on the order itself
    sources.append({"sha256": wo["manual_sha256"]})
    assert len(sources) == 4 and all(s["sha256"] == SHA for s in sources)


def test_server_exposes_manifest_resource_and_list_tool(tools):
    pytest.importorskip("mcp")
    from om_copilot.mcp_server import build_server

    server = build_server(tools)
    assert "list_manuals" in {t.name for t in asyncio.run(server.list_tools())}
    assert [str(r.uri) for r in asyncio.run(server.list_resources())] == ["manuals://manifest"]
    body = asyncio.run(server.read_resource("manuals://manifest"))
    assert SHA in str(body)
