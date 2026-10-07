from __future__ import annotations

import errno
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files import (
    workspace_descriptor_access as descriptor_access,
)
from pantaray_agents.tools.files.text_lines import read_text_descriptor_lines
from pantaray_agents.tools.files.workspace_descriptor_access import (
    glob_workspace_files,
    open_workspace_file_descriptor,
    scan_workspace_entries,
)


def _replace_with_outside_symlink(
    *,
    path: Path,
    outside: Path,
    renamed: Path,
) -> None:
    path.rename(renamed)
    path.symlink_to(outside, target_is_directory=outside.is_dir())


def _read_descriptor(descriptor: int) -> str:
    return read_text_descriptor_lines(
        descriptor=descriptor,
        offset=1,
        limit=10,
    ).content


def test_file_open_stays_on_parent_descriptor_after_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    (parent / "document.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.txt").write_text("outside secret\n", encoding="utf-8")
    renamed = root / "original-parent"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "document.txt" and dir_fd is not None and not swapped:
            swapped = True
            _replace_with_outside_symlink(
                path=parent,
                outside=outside,
                renamed=renamed,
            )
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)

    descriptor = open_workspace_file_descriptor(
        root_path=root,
        relative_path="parent/document.txt",
    )
    try:
        assert _read_descriptor(descriptor) == "inside\n"
    finally:
        os.close(descriptor)
    assert swapped is True


def test_directory_component_swap_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    (parent / "document.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.txt").write_text("outside secret\n", encoding="utf-8")
    renamed = root / "original-parent"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "parent" and dir_fd is not None and not swapped:
            swapped = True
            _replace_with_outside_symlink(
                path=parent,
                outside=outside,
                renamed=renamed,
            )
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)

    with pytest.raises(BrokerPolicyError, match="symlink"):
        open_workspace_file_descriptor(
            root_path=root,
            relative_path="parent/document.txt",
        )
    assert swapped is True


def test_list_uses_open_base_descriptor_after_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    base = root / "base"
    base.mkdir(parents=True)
    (base / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    for index in range(20):
        (outside / f"outside-{index}.txt").write_text("secret\n", encoding="utf-8")
    renamed = root / "original-base"
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            _replace_with_outside_symlink(
                path=base,
                outside=outside,
                renamed=renamed,
            )
        return real_scandir(path)

    monkeypatch.setattr(descriptor_access.os, "scandir", racing_scandir)

    result = scan_workspace_entries(
        root_path=root,
        base_path="base",
        max_depth=1,
        limit=100,
    )

    assert [entry.root_relative_path for entry in result.entries] == ["base/inside.txt"]
    assert result.truncation_reason is None
    assert swapped is True


def test_final_file_symlink_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "document.txt"
    source.write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    renamed = root / "original-document.txt"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "document.txt" and dir_fd is not None and not swapped:
            swapped = True
            _replace_with_outside_symlink(
                path=source,
                outside=outside,
                renamed=renamed,
            )
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)

    with pytest.raises(BrokerPolicyError, match="symlink"):
        open_workspace_file_descriptor(
            root_path=root,
            relative_path="document.txt",
        )
    assert swapped is True


def _assert_no_descriptor_leak(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[], object],
) -> None:
    real_open = os.open
    real_close = os.close
    opened: Counter[int] = Counter()
    closed: Counter[int] = Counter()

    def tracked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened[descriptor] += 1
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed[descriptor] += 1
        real_close(descriptor)

    monkeypatch.setattr(descriptor_access.os, "open", tracked_open)
    monkeypatch.setattr(descriptor_access.os, "close", tracked_close)
    operation()
    assert opened == closed


def test_limit_closes_all_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(3):
        (root / f"file-{index}.txt").write_text("content\n", encoding="utf-8")

    def scan() -> None:
        result = scan_workspace_entries(
            root_path=root,
            base_path=".",
            max_depth=1,
            limit=1,
        )
        assert result.truncation_reason == "limit"

    _assert_no_descriptor_leak(monkeypatch, scan)


def test_success_closes_all_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("content\n", encoding="utf-8")

    def scan() -> None:
        result = scan_workspace_entries(
            root_path=root,
            base_path=".",
            max_depth=1,
            limit=10,
        )
        assert [entry.root_relative_path for entry in result.entries] == ["file.txt"]

    _assert_no_descriptor_leak(monkeypatch, scan)


def test_scandir_error_closes_all_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    real_scandir = os.scandir

    def fail_scandir(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ):
        if isinstance(path, int):
            raise OSError(errno.EIO, "forced scandir failure")
        return real_scandir(path)

    monkeypatch.setattr(descriptor_access.os, "scandir", fail_scandir)

    def scan() -> None:
        with pytest.raises(OSError) as exc_info:
            scan_workspace_entries(
                root_path=root,
                base_path=".",
                max_depth=1,
                limit=10,
            )
        assert exc_info.value.errno == errno.EIO

    _assert_no_descriptor_leak(monkeypatch, scan)


def test_glob_preserves_star_recursive_hidden_and_double_star_rules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "top.py").write_text("top\n", encoding="utf-8")
    (root / ".hidden").write_text("hidden\n", encoding="utf-8")
    (nested / "app.py").write_text("app\n", encoding="utf-8")
    (nested / "note.txt").write_text("note\n", encoding="utf-8")

    star = glob_workspace_files(
        root_path=root,
        base_path=".",
        pattern="*",
        limit=20,
    )
    python = glob_workspace_files(
        root_path=root,
        base_path=".",
        pattern="**/*.py",
        limit=20,
    )

    assert [entry.root_relative_path for entry in star.entries] == [
        ".hidden",
        "nested/app.py",
        "nested/note.txt",
        "top.py",
    ]
    assert [entry.root_relative_path for entry in python.entries] == [
        "nested/app.py",
        "top.py",
    ]


def test_glob_remains_on_base_descriptor_after_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    base = root / "base"
    base.mkdir(parents=True)
    (base / "inside.py").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.py").write_text("secret\n", encoding="utf-8")
    real_scandir = os.scandir
    swapped = False

    def racing_scandir(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ):
        nonlocal swapped
        if isinstance(path, int) and not swapped:
            swapped = True
            _replace_with_outside_symlink(
                path=base,
                outside=outside,
                renamed=root / "original-base",
            )
        return real_scandir(path)

    monkeypatch.setattr(descriptor_access.os, "scandir", racing_scandir)

    result = glob_workspace_files(
        root_path=root,
        base_path="base",
        pattern="**/*.py",
        limit=20,
    )

    assert [entry.root_relative_path for entry in result.entries] == ["base/inside.py"]
    assert "outside.py" not in str(result)


def test_missing_and_symlink_paths_close_all_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    def rejected_scans() -> None:
        for base_path in ("missing", "link"):
            with pytest.raises(BrokerPolicyError):
                scan_workspace_entries(
                    root_path=root,
                    base_path=base_path,
                    max_depth=1,
                    limit=10,
                )

    _assert_no_descriptor_leak(monkeypatch, rejected_scans)
