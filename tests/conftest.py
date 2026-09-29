import pytest

from revv.demo import DEMO_REF, DemoBackend
from revv.ui.app import RevvApp


@pytest.fixture
def backend() -> DemoBackend:
    return DemoBackend(latency=0)


@pytest.fixture
def app(backend: DemoBackend) -> RevvApp:
    return RevvApp(backend, target=DEMO_REF, repo=DEMO_REF.repo)
