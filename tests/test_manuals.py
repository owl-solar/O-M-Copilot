from pathlib import Path
from types import SimpleNamespace

import pytest

from om_copilot import config, manuals
from om_copilot.workorder import priority_of


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_COPILOT_LOCAL_CONFIG", str(tmp_path / "local.json"))
    monkeypatch.setenv("OM_COPILOT_RAW_DIR", str(tmp_path / "raw"))
    return tmp_path


def _doc(name, codes):
    return SimpleNamespace(filename=name, sha256="ab" * 32, chunks=[1], tables=[], warnings=[],
                           data={"pages": 10, "fault_codes": codes})


def test_defaults_and_unknown_file_falls_back_to_the_stem(local):
    assert config.model_of("SG305HX_SG320HX-20_SG350HX-20 User Manual.pdf") == "SG350HX-20"
    assert config.model_of("Some Maker Manual.pdf") == "Some Maker Manual"
    assert priority_of("Major") == "P2" and priority_of("odd") == "P3"


def test_register_model_and_local_priority_override_defaults(local):
    config.register_model("Some Maker Manual", "ABC-1000")
    assert config.model_of("Some Maker Manual.pdf") == "ABC-1000"
    config.register_model("some maker manual", "ABC-2000")  # same match: replaced, not duplicated
    assert config.model_of("Some Maker Manual.pdf") == "ABC-2000"
    assert len(config.load()["models"]) == 4
    (local / "local.json").write_text('{"priority": {"severe": "P1"}}', encoding="utf-8")
    config._merged.cache_clear()
    assert priority_of("Severe") == "P1" and priority_of("Major") == "P2"


def test_inspect_flags_missing_codes_unknown_severity_and_missing_label(local):
    info = manuals.inspect(_doc("Other.pdf", []))
    assert info["fault_codes"] == 0 and any("No fault-code table" in n for n in info["notes"])
    assert any("No model label" in n for n in info["notes"])
    info = manuals.inspect(_doc("Other.pdf", [{"code": "1", "page": 3, "severity": "Fatal"}]))
    assert info["unknown_severities"] == ["fatal"] and any("priority table" in n for n in info["notes"])


def test_add_manual_copies_reads_and_saves_the_label(local, monkeypatch):
    src = local / "download" / "Other Maker Manual.pdf"
    src.parent.mkdir()
    src.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(manuals, "ingest", lambda p: _doc(Path(p).name, [{"code": "7", "page": 5}]))
    _, info, dest = manuals.add_manual(src, "ABC-1000")
    assert dest == local / "raw" / "Other Maker Manual.pdf" and dest.read_bytes() == b"%PDF-fake"
    assert info["fault_codes"] == 1 and info["model"] == "ABC-1000" and info["notes"] == []
    assert config.model_of("Other Maker Manual.pdf") == "ABC-1000"
    with pytest.raises(FileNotFoundError):
        manuals.add_manual(local / "missing.pdf")
