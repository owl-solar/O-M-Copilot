"""Corpus loading and summary, on generated files only (no manufacturer documents)."""
import pytest
from design_ingest import Document

from om_copilot.cli import main
from om_copilot.corpus import load_documents, summarize


def _doc(fault_codes) -> Document:
    return Document(
        source="x", filename="m.pdf", file_type="pdf", sha256="0" * 64, size_bytes=1,
        parser="pdf", parser_version="1", data={"pages": 10, "fault_codes": fault_codes},
    )


def test_summarize_counts_codes_and_pages():
    s = summarize(_doc([{"code": "1", "page": 7}, {"code": "2", "page": 9}]))
    assert s["fault_codes"] == 2 and s["fault_pages"] == "7-9" and s["pages"] == 10


def test_summarize_without_codes():
    s = summarize(_doc([]))
    assert s["fault_codes"] == 0 and s["fault_pages"] == "-"


def test_load_documents_reads_generated_docx(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Troubleshooting", level=1)
    d.add_paragraph("Check the AC cable.")
    d.save(tmp_path / "m.docx")
    docs = load_documents(tmp_path)
    assert [x.filename for x in docs] == ["m.docx"]


def test_missing_folder_has_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="docs/sources.md"):
        load_documents(tmp_path / "nope")


def test_cli_ingest_prints_summary(tmp_path, capsys):
    import docx

    docx.Document().save(tmp_path / "empty.docx")
    assert main(["ingest", str(tmp_path)]) == 0
    assert "empty.docx" in capsys.readouterr().out
