from pathlib import Path

import pytest


@pytest.fixture
def target_path(tmp_path: Path) -> Path:
    return tmp_path / "target.sqlite3"
