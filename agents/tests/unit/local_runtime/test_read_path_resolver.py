from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerContext,
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import ManifestRoot
from pantaray_agents.local_runtime.tooling.brokering.read_path_resolver import (
    READ_PATH_NOT_FOUND,
    READ_SCOPE_DENIED,
    resolve_read_local_path,
    resolve_read_target,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)


def test_relative_path_resolves_from_execution_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note_path = workspace / "notes.txt"
    note_path.write_text("note\n", encoding="utf-8")
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    target = resolve_read_target(
        context=cast(BrokerContext, context),
        raw_path="notes.txt",
    )

    assert target.display_path == str(note_path)
    assert target.real_path == note_path.resolve()


def test_read_accepts_literal_glob_metacharacters_in_file_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note_path = workspace / "notes[final]?.txt"
    note_path.write_text("note\n", encoding="utf-8")
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    target = resolve_read_target(
        context=cast(BrokerContext, context),
        raw_path=note_path.name,
    )

    assert target.display_path == str(note_path)
    assert target.real_path == note_path.resolve()


def test_absolute_path_respects_more_specific_unreadable_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private = workspace / "private"
    private_file = private / "secret.txt"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("secret\n", encoding="utf-8")
    context = _context(
        cwd_path=workspace,
        roots=(
            _root(real_path=private, can_read=False),
            _root(real_path=workspace, can_read=True),
        ),
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_read_target(
            context=cast(BrokerContext, context),
            raw_path=str(private_file),
        )

    assert exc_info.value.code == "READ_SCOPE_DENIED"


def test_absolute_symlink_escape_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret\n", encoding="utf-8")
    link_path = workspace / "secret-link.txt"
    link_path.symlink_to(outside_file)
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_read_target(
            context=cast(BrokerContext, context),
            raw_path=str(link_path),
        )

    assert exc_info.value.code == "READ_PATH_DENIED"


def test_workspace_scope_missing_outside_path_does_not_suggest_parent_entries(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-nearby.txt").write_text("secret\n", encoding="utf-8")
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_read_target(
            context=cast(BrokerContext, context),
            raw_path=str(outside / "secret-typo.txt"),
        )

    assert exc_info.value.code == "READ_SCOPE_DENIED"
    assert "secret-nearby.txt" not in str(exc_info.value)


def test_workspace_scope_missing_inside_path_returns_not_found(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_read_target(
            context=cast(BrokerContext, context),
            raw_path=str(workspace / "missing" / "notes.txt"),
        )

    assert exc_info.value.code == READ_PATH_NOT_FOUND
    assert "READ_SCOPE_DENIED" not in str(exc_info.value)


def test_workspace_scope_preserves_in_workspace_shape_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_file = workspace / "notes.txt"
    target_file.write_text("hello", encoding="utf-8")
    context = _context(cwd_path=workspace, roots=(_root(real_path=workspace),))

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_read_local_path(
            context=cast(BrokerContext, context),
            raw_path=str(target_file),
            must_exist=True,
            must_be_dir=True,
        )

    assert exc_info.value.code != READ_SCOPE_DENIED
    assert str(exc_info.value) == "path must reference an existing directory"


def test_full_access_scope_allows_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    context = _context(
        cwd_path=workspace,
        roots=(_root(real_path=workspace),),
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )

    target = resolve_read_target(
        context=cast(BrokerContext, context),
        raw_path=str(outside_file),
    )

    assert target.display_path == str(outside_file)
    assert target.real_path == outside_file.resolve()


def _context(
    *,
    cwd_path: Path,
    roots: tuple[ManifestRoot, ...],
    read_access_scope: str = READ_ACCESS_SCOPE_WORKSPACE,
) -> SimpleNamespace:
    return SimpleNamespace(
        execution_session=SimpleNamespace(cwd_path=str(cwd_path.resolve())),
        scratch_root_path=cwd_path.resolve(),
        tool_definition=SimpleNamespace(tool_id="read"),
        path_access_kind="read",
        manifest_roots=roots,
        read_access_scope=read_access_scope,
        db_path=cwd_path.resolve().parent / "app-data" / "runtime.db",
    )


def _root(
    *,
    real_path: Path,
    can_read: bool = True,
) -> ManifestRoot:
    real_path.mkdir(parents=True, exist_ok=True)
    return ManifestRoot(
        root_id=f"root:{real_path.name}",
        manifest_id="manifest:action-1",
        source_type="folder",
        display_name=real_path.name,
        canonical_real_path=real_path.resolve(),
        real_path=real_path.resolve(),
        can_read=can_read,
        can_apply_patch=True,
        can_process_read=True,
        can_process_write=True,
    )
