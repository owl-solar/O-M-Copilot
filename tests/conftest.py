import pytest


@pytest.fixture(autouse=True)
def _no_local_config(tmp_path, monkeypatch):
    """Tests use the shipped defaults, never a developer's own data/config.local.json."""
    monkeypatch.setenv("OM_COPILOT_LOCAL_CONFIG", str(tmp_path / "no_local_config.json"))
