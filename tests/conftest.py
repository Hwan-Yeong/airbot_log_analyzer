import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session")
def demo_zip(tmp_path_factory):
    from make_demo_data import make_demo
    return make_demo(tmp_path_factory.mktemp("demo"))


@pytest.fixture()
def ws(tmp_path):
    from airbotlog.workspace import Workspace
    w = Workspace(temp_root=tmp_path / "tmp")
    yield w
    w.cleanup()


@pytest.fixture()
def settings(tmp_path):
    from airbotlog.settings import Settings
    return Settings(user_path=tmp_path / "user_settings.yaml")
