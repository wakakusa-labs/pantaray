from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.manifest_paths import (
    ManifestRoot,
    resolve_local_path,
    resolve_process_cwd,
    resolve_tool_results_root,
)


def test_resolve_local_path_accepts_cwd_relative_workspace_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    file_path = workspace / "notes.txt"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("note\n", encoding="utf-8")

    resolved = resolve_local_path(
        roots=(_root(real_path=workspace),),
        raw_path="notes.txt",
        cwd_path=workspace,
        capability="read",
        must_exist=True,
    )

    assert resolved.path == file_path.resolve()
    assert resolved.root_relative_path == "notes.txt"


def test_resolve_local_path_denies_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link_path = workspace / "outside-link.txt"
    link_path.symlink_to(outside)

    with pytest.raises(BrokerPolicyError):
        resolve_local_path(
            roots=(_root(real_path=workspace),),
            raw_path=str(link_path),
            cwd_path=workspace,
            capability="read",
            must_exist=True,
        )


def test_resolve_local_path_rejects_registered_root_retargeting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    moved_workspace = tmp_path / "workspace-original"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    root = _root(real_path=workspace)
    workspace.rename(moved_workspace)
    workspace.symlink_to(outside, target_is_directory=True)
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError, match="approved target"):
        resolve_local_path(
            roots=(root,),
            raw_path=str(workspace / "secret.txt"),
            cwd_path=workspace,
            capability="read",
            must_exist=True,
        )


def test_resolve_local_path_accepts_original_registered_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "workspace-target"
    registered_path = tmp_path / "workspace-link"
    target.mkdir()
    registered_path.symlink_to(target, target_is_directory=True)
    file_path = target / "notes.txt"
    file_path.write_text("note\n", encoding="utf-8")
    root = ManifestRoot(
        root_id="root:workspace",
        manifest_id="manifest:action-1",
        source_type="folder",
        display_name="workspace",
        canonical_real_path=target.resolve(),
        real_path=registered_path,
        can_read=True,
        can_apply_patch=True,
        can_process_read=True,
        can_process_write=True,
    )

    resolved = resolve_local_path(
        roots=(root,),
        raw_path=str(registered_path / "notes.txt"),
        cwd_path=registered_path,
        capability="read",
        must_exist=True,
    )

    assert resolved.path == file_path


def test_resolve_process_cwd_uses_nearest_git_root_inside_registered_folder(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "work"
    repo_root = workspace / "repo-a"
    repo_child = repo_root / "packages" / "app"
    repo_child.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    resolved = resolve_process_cwd(
        roots=(_root(real_path=workspace),),
        raw_cwd=str(repo_child),
        default_cwd=workspace,
    )

    assert resolved.path == repo_child.resolve()
    assert resolved.process_scope_root == repo_root.resolve()


def test_resolve_process_cwd_uses_registered_folder_when_no_git_root_exists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "documents"
    child = workspace / "reports" / "2026"
    child.mkdir(parents=True)

    resolved = resolve_process_cwd(
        roots=(_root(real_path=workspace),),
        raw_cwd="reports/2026",
        default_cwd=workspace,
    )

    assert resolved.path == child.resolve()
    assert resolved.process_scope_root == workspace.resolve()


def test_resolve_process_cwd_none_defaults_to_current_workspace_cwd(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    resolved = resolve_process_cwd(
        roots=(_root(real_path=workspace),),
        raw_cwd=None,
        default_cwd=workspace,
    )

    assert resolved.path == workspace.resolve()
    assert resolved.process_scope_root == workspace.resolve()


def test_resolve_tool_results_root_uses_action_root_id(tmp_path: Path) -> None:
    scratch = _root(real_path=tmp_path / "scratch")
    tool_results = replace(
        _root(real_path=tmp_path / "tool-results"),
        root_id="root:action-1:tool-results",
    )

    resolved = resolve_tool_results_root(
        roots=(scratch, tool_results),
        action_id="action-1",
    )

    assert resolved == tool_results.canonical_real_path


def test_resolve_tool_results_root_requires_exactly_one_root(tmp_path: Path) -> None:
    with pytest.raises(BrokerPolicyError, match="tool-results manifest root"):
        resolve_tool_results_root(
            roots=(_root(real_path=tmp_path / "scratch"),),
            action_id="action-1",
        )


def _root(
    *,
    real_path: Path,
    can_process_write: bool = True,
) -> ManifestRoot:
    real_path.mkdir(parents=True, exist_ok=True)
    return ManifestRoot(
        root_id=f"root:{real_path.name}",
        manifest_id="manifest:action-1",
        source_type="folder",
        display_name=real_path.name,
        canonical_real_path=real_path.resolve(),
        real_path=real_path.resolve(),
        can_read=True,
        can_apply_patch=True,
        can_process_read=True,
        can_process_write=can_process_write,
    )
