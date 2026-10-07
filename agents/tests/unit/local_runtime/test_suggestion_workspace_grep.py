"""The Suggestion grep runs the shared ripgrep core, bound to one readable root."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker_discovery_ripgrep
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_discovery_ripgrep import (
    RIPGREP_TRUSTED_PATH,
    RipgrepRunResult,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files.access import ReadOnlyFileAccess
from pantaray_agents.tools.files.roots import WorkspaceReadRoot

from .test_suggestion_research_tools import (
    _bootstrap_db,
    _register_workspace,
    _snapshot,
)

pytestmark = pytest.mark.skipif(
    shutil.which("rg", path=RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)
_SANDBOXED = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires Darwin with /usr/bin/sandbox-exec",
)
SENTINEL = "needle from outside the readable root"


def _reader(root: Path) -> ReadOnlyFileAccess:
    storage = root.parent / "app-data"
    return ReadOnlyFileAccess(
        roots=(WorkspaceReadRoot("workspace", "Workspace", root, (storage,)),)
    )


def _grep(
    reader: ReadOnlyFileAccess,
    *,
    root_id: str = "workspace",
    base_path: str = ".",
    include_glob: str | None = None,
    offset: int = 1,
    max_matches: int = 10,
) -> dict[str, JSONValue]:
    return reader.grep(
        root_id=root_id,
        base_path=base_path,
        pattern="needle",
        include_glob=include_glob,
        offset=offset,
        max_matches=max_matches,
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_grep_searches_a_line_longer_than_eight_mebibytes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "wide.txt").write_text(
        "x" * (8 * 1024 * 1024 + 1) + " needle\nneedle 2\n", encoding="utf-8"
    )

    result = _grep(_reader(root))

    # The match lies past ripgrep's preview, so the excerpt is the line's start.
    assert result["matches"] == [
        {"path": "wide.txt", "line_number": 1, "line": "x" * 500 + "…"},
        {"path": "wide.txt", "line_number": 2, "line": "needle 2"},
    ]
    assert "over 64 KB into the line" in str(result["warning"])
    assert result["truncated"] is False


def test_grep_pages_root_relative_matches_in_path_order(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    docs = root / "docs"
    (docs / "a").mkdir(parents=True)
    for name in ("c.txt", "b.txt", "a/z.txt"):
        (docs / name).write_text("needle one\nneedle two\n", encoding="utf-8")
    reader = _reader(root)

    first = _grep(reader, base_path="docs", include_glob="*.txt", max_matches=4)
    pages = [first]
    while (next_offset := pages[-1]["next_offset"]) is not None:
        assert isinstance(next_offset, int)
        pages.append(
            _grep(
                reader,
                base_path="docs",
                include_glob="*.txt",
                offset=next_offset,
                max_matches=4,
            )
        )

    assert first["truncation_reason"] == "page_limit"
    assert first["retry_hint"] == "Continue with offset=next_offset."
    assert [
        (match["path"], match["line_number"])  # type: ignore[index]
        for page in pages
        for match in page["matches"]  # type: ignore[union-attr]
    ] == [
        (f"docs/{name}", line)
        for name in ("a/z.txt", "b.txt", "c.txt")
        for line in (1, 2)
    ]
    assert pages[-1]["truncated"] is False


def test_grep_bounds_a_long_multibyte_line(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    line = "needle" + "あ" * 600
    (root / "matches.txt").write_text(line + "\n", encoding="utf-8")

    result = _grep(_reader(root), max_matches=1)

    assert result["matches"] == [
        {"path": "matches.txt", "line_number": 1, "line": line[:500] + "…"}
    ]
    assert "excerpt around their first match" in str(result["warning"])
    assert "offset=line_number" in str(result["retry_hint"])


def test_grep_reports_what_it_passed_over(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "docs" / "deep" / "note.txt").write_text("needle deep\n")
    (root / "big.log").write_bytes(b"x\n" * (1024 * 1024) + b"needle late\n")
    (root / "blob.bin").write_bytes(b"needle\0")
    (root / "latin1.txt").write_bytes(b"needle caf\xe9\n")
    (root / "link.txt").symlink_to(root / "big.log")
    unreadable = root / "unreadable.txt"
    unreadable.write_text("needle hidden\n")
    unreadable.chmod(0)

    try:
        result = _grep(_reader(root))
    finally:
        unreadable.chmod(0o600)

    assert result["matches"] == [
        {"path": "big.log", "line_number": 1024 * 1024 + 1, "line": "needle late"},
        {"path": "docs/deep/note.txt", "line_number": 1, "line": "needle deep"},
        {"path": "latin1.txt", "line_number": 1, "line": "needle caf�"},
    ]
    assert result["skipped_files"] == 1
    warning = str(result["warning"])
    assert "1 path(s) could not be read" in warning
    assert "unreadable.txt" in warning
    assert "1 binary file(s) also match" in warning
    assert "blob.bin" in warning


def test_grep_refuses_a_base_reached_through_a_symlink(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(f"{SENTINEL}\n", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BrokerPolicyError, match="symlink"):
        _grep(_reader(root), base_path="link")


def test_grep_keeps_out_of_private_app_storage_in_a_parent_folder(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    storage = db_path.parent
    records = storage / "records"
    records.mkdir()
    for index in range(60):
        (records / f"{index}.txt").write_text("needle secret\n", encoding="utf-8")
    # Reading this would be reported as skipped, so the search must not reach it.
    unreadable = storage / "unreadable.txt"
    unreadable.write_text("needle secret\n", encoding="utf-8")
    unreadable.chmod(0)
    (tmp_path / "sibling.txt").write_text("needle sibling\n", encoding="utf-8")
    _register_workspace(db_path=db_path, root=tmp_path)
    snapshot = _snapshot(db_path=db_path)
    reader = ReadOnlyFileAccess(roots=snapshot.roots)
    root_id = snapshot.roots[-1].root_id

    try:
        result = _grep(reader, root_id=root_id, max_matches=50)
        with pytest.raises(BrokerPolicyError, match="private app storage"):
            _grep(reader, root_id=root_id, base_path=storage.name)
    finally:
        unreadable.chmod(0o600)

    assert [match["path"] for match in result["matches"]] == ["sibling.txt"]  # type: ignore[index, union-attr]
    assert result["truncated"] is False
    assert result["warning"] is None
    assert result["skipped_files"] == 0


def _swap_for_link_while_ripgrep_runs(
    monkeypatch: pytest.MonkeyPatch, *, directory: Path, target: Path
) -> None:
    """Point ``directory`` at ``target`` only while ripgrep reads it."""

    run = broker_discovery_ripgrep._run_ripgrep_lines

    def swapped(**kwargs: object) -> RipgrepRunResult:
        shutil.rmtree(directory)
        directory.symlink_to(target, target_is_directory=True)
        try:
            return run(**kwargs)  # type: ignore[arg-type]
        finally:
            directory.unlink()
            directory.mkdir()

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", swapped)


@_SANDBOXED
def test_base_swapped_for_outward_link_reads_nothing_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    searched = root / "searched"
    searched.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(f"{SENTINEL}\n", encoding="utf-8")
    _swap_for_link_while_ripgrep_runs(monkeypatch, directory=searched, target=outside)

    result = _grep(_reader(root), base_path="searched")

    assert result["matches"] == []
    assert SENTINEL not in str(result)
    assert "secret.txt" not in str(result)


@_SANDBOXED
def test_base_swapped_for_link_into_private_app_storage_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _bootstrap_db(tmp_path)
    (db_path.parent / "secret.txt").write_text(f"{SENTINEL}\n", encoding="utf-8")
    searched = tmp_path / "searched"
    searched.mkdir()
    _register_workspace(db_path=db_path, root=tmp_path)
    snapshot = _snapshot(db_path=db_path)
    _swap_for_link_while_ripgrep_runs(
        monkeypatch, directory=searched, target=db_path.parent
    )

    result = _grep(
        ReadOnlyFileAccess(roots=snapshot.roots),
        root_id=snapshot.roots[-1].root_id,
        base_path="searched",
    )

    assert result["matches"] == []
    assert SENTINEL not in str(result)
    assert "secret.txt" not in str(result)
