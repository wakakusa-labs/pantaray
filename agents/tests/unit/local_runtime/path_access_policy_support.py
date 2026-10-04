from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import ManifestRoot
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_WORKSPACE,
    update_read_access_scope,
)

from .action_seed import insert_agent_action
from .broker_test_support import _seed_broker_actor_process
from .migrated_db import prepare_test_database

USER_ID = "user-1"
ACTION_ID = "action-1"
NOW = "2026-03-23T00:00:00Z"


def bootstrap_path_policy_runtime_db(
    tmp_path: Path,
    *,
    allowed_tool_ids: tuple[str, ...],
    read_access_scope: str = READ_ACCESS_SCOPE_WORKSPACE,
) -> tuple[Path, object]:
    # App storage lives apart from the files a test reads as the user's own.
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path, action_id=ACTION_ID, created_at=NOW)
    _seed_broker_actor_process(db_path)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        read_access_scope=read_access_scope,
        now=NOW,
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        action_id=ACTION_ID,
        started_at=NOW,
        allowed_tool_ids=allowed_tool_ids,
    )
    return db_path, context


def build_path_policy_context(
    *,
    cwd_path: Path,
    path_access_kind: str,
    manifest_roots: tuple[ManifestRoot, ...] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        execution_session=SimpleNamespace(cwd_path=str(cwd_path.resolve())),
        scratch_root_path=cwd_path.resolve(),
        tool_definition=SimpleNamespace(tool_id="test_tool"),
        path_access_kind=path_access_kind,
        manifest_roots=manifest_roots
        or (build_manifest_root(path=cwd_path, name=cwd_path.name),),
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        db_path=cwd_path.resolve().parent / "app-data" / "runtime.db",
    )


def build_manifest_root(
    *,
    path: Path,
    name: str,
    can_apply_patch: bool = True,
    can_process_read: bool = True,
    can_process_write: bool = True,
) -> ManifestRoot:
    return ManifestRoot(
        root_id=f"root:{name}",
        manifest_id="manifest:action-1",
        source_type="folder",
        display_name=name,
        canonical_real_path=path.resolve(),
        real_path=path.resolve(),
        can_read=True,
        can_apply_patch=can_apply_patch,
        can_process_read=can_process_read,
        can_process_write=can_process_write,
    )
