from pathlib import Path

import pytest

from app.config import DATA_DIR
from app.ingest import SOURCES_FILE
from app.main import FIXTURES


def test_data_files_resolve_independent_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    for path in (DATA_DIR, FIXTURES, SOURCES_FILE):
        assert path.is_absolute()
        assert path.exists()
