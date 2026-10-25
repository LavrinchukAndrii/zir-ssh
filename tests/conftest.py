import pytest

from zir.i18n import set_language


@pytest.fixture(autouse=True)
def english_by_default(tmp_path, monkeypatch):
    """Most tests assert English text; test_i18n.py switches to Ukrainian itself.

    Preferences and saved state go to a temp dir so tests never touch the real ones.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("ZIR_LANG", raising=False)
    for legacy in ("SDECK_LANG", "SDECK_TERMINAL"):
        monkeypatch.delenv(legacy, raising=False)
    set_language("en")
    yield
    set_language("en")
