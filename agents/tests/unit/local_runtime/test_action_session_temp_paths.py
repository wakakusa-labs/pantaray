from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling import action_session_temp_paths
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    ActionSessionTempPathError,
    create_private_temp_dir,
    private_temp_parent,
    resolve_action_storage_paths,
    resolve_private_temp_root,
    validate_action_storage_component,
)
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    app_owned_roots,
)

USER_ID = "user-1"
ACTION_ID = "action-1"


def test_action_storage_paths_are_derived_from_one_canonical_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_base = tmp_path / "first"
    second_base = tmp_path / "second"
    resolved_bases = iter((first_base, second_base))
    call_count = 0

    def resolve_storage_base(*, db_path: Path) -> Path:
        nonlocal call_count
        del db_path
        call_count += 1
        return next(resolved_bases)

    monkeypatch.setattr(
        action_session_temp_paths,
        "resolve_local_runtime_storage_base",
        resolve_storage_base,
    )

    paths = resolve_action_storage_paths(
        db_path=tmp_path / "runtime.db",
        user_id=USER_ID,
        action_id=ACTION_ID,
    )

    expected_root = (
        first_base / "local_runtime_workspaces" / "scratch" / USER_ID / ACTION_ID
    )
    assert call_count == 1
    assert paths.storage_base == first_base
    assert paths.action_root == expected_root
    assert paths.workspace == expected_root / "scratch"
    assert paths.tool_results == expected_root / "tool-results"
    assert paths.session_temp_root == expected_root / "scratch" / ".runtime-temp"


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b", "a\0b"])
def test_action_storage_component_rejects_non_component_identity(value: str) -> None:
    with pytest.raises(ActionSessionTempPathError):
        validate_action_storage_component(field_name="identity", value=value)


def test_command_temp_dir_has_no_whitespace_and_stays_private(tmp_path: Path) -> None:
    # The app's data dir is ~/Library/Application Support/pantaray.
    db_path = tmp_path / "Application Support" / "pantaray" / "runtime.db"
    db_path.parent.mkdir(parents=True)
    temp_dir = create_private_temp_dir(db_path=db_path, prefix="probe-")
    try:
        # Unquoted $TMPDIR in a command splits on whitespace.
        assert not any(character.isspace() for character in str(temp_dir))
        assert temp_dir == temp_dir.resolve()
        assert stat.S_IMODE(temp_dir.stat().st_mode) == 0o700
        # The seatbelt denies private storage, every profile's included, to
        # every other command.
        assert temp_dir.parent.parent == private_temp_parent()
        assert private_temp_parent() in app_owned_roots(db_path)
    finally:
        shutil.rmtree(temp_dir)


@pytest.mark.parametrize("planted", ["parent", "root"])
def test_command_temp_dir_refuses_a_link_planted_at_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    # An unsandboxed command can leave a link that mkdir(exist_ok) would follow.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db_path = tmp_path / "storage" / "runtime.db"
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = (
        private_temp_parent()
        if planted == "parent"
        else resolve_private_temp_root(db_path=db_path)
    )
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ActionSessionTempPathError):
        create_private_temp_dir(db_path=db_path, prefix="probe-")
    assert list(target.iterdir()) == []
