from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

UI = Path(__file__).resolve().parents[1] / "src" / "om_copilot" / "ui.py"


def test_page_says_what_is_missing_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_COPILOT_RAW_DIR", str(tmp_path / "missing"))
    at = AppTest.from_file(str(UI), default_timeout=30).run()
    assert not at.exception and "not found" in at.error[0].value

    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("OM_COPILOT_RAW_DIR", str(tmp_path / "empty"))
    at = AppTest.from_file(str(UI), default_timeout=30).run()
    assert not at.exception and "No manuals" in at.error[0].value
