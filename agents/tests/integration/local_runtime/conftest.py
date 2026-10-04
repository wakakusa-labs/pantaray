from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def outside_temp_path() -> Iterator[Path]:
    """A fresh directory that no sandboxed command may reach by default.

    pytest's tmp_path lies in the OS temp dir, which commands may read and write.
    """

    path = Path(tempfile.mkdtemp(dir=Path.home() / "Library" / "Caches")).resolve()
    yield path
    shutil.rmtree(path)
