import pytest

from revv import config, filters
from revv import summary as summarizer
from revv.demo import DEMO_REF, DemoBackend
from revv.ui.app import RevvApp


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/revv or ~/.cache/revv from tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "_nicknames", None)
    # ...and never run the real claude: a test that summarizes brings a fake one
    monkeypatch.setattr(summarizer, "executable", lambda: None)
    monkeypatch.setattr(summarizer, "_signed_in", None)
    filters.reload()


@pytest.fixture
def backend() -> DemoBackend:
    return DemoBackend(latency=0)


@pytest.fixture
def app(backend: DemoBackend) -> RevvApp:
    return RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo)
