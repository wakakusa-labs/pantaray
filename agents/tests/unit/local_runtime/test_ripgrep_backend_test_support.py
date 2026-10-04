from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.local_runtime import ripgrep_backend_test_support
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.tooling.brokering import broker_discovery


def test_fake_glob_backend_stops_before_next_directory_after_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    (tmp_path / "first.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("needle\n", encoding="utf-8")

    def fake_walk(root: Path, *, followlinks: bool = False):
        del followlinks
        yield (str(root), ["unvisited"], ["first.txt", "second.txt"])
        raise AssertionError("fake backend consumed the next directory")

    monkeypatch.setattr(ripgrep_backend_test_support.os, "walk", fake_walk)

    result = broker_discovery.run_ripgrep_files(
        cwd=tmp_path,
        sandbox_profile="",
        glob_pattern="*.txt",
        limit=1,
    )

    assert result.relative_paths == ("first.txt",)
    assert result.truncated is True
    assert result.truncation_reason == "limit"


def test_fake_grep_backend_stops_before_next_directory_after_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    (tmp_path / "first.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("needle\n", encoding="utf-8")

    def fake_walk(root: Path, *, followlinks: bool = False):
        del followlinks
        yield (str(root), ["unvisited"], ["first.txt", "second.txt"])
        raise AssertionError("fake backend consumed the next directory")

    monkeypatch.setattr(ripgrep_backend_test_support.os, "walk", fake_walk)

    result = broker_discovery.run_ripgrep_grep(
        cwd=tmp_path,
        sandbox_profile="",
        pattern="needle",
        include_glob="*.txt",
        max_matches=1,
    )

    assert [match.relative_path for match in result.matches] == ["first.txt"]
    assert result.truncated is True
    assert result.truncation_reason == "limit"
