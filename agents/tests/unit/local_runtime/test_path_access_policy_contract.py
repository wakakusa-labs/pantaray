from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerApprovalRequiredError,
    BrokerContext,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_registry import (
    BROKER_TOOL_REGISTRY,
    validate_broker_registry,
)
from pantaray_agents.local_runtime.tooling.brokering.tool_path_policy import (
    EXEC_CWD_DENIED,
    EXEC_CWD_NOT_FOUND,
    READ_PATH_NOT_FOUND,
    READ_SCOPE_DENIED,
    SUGGESTION_SCAN_LIMIT,
    WRITE_PATH_DENIED,
    WRITE_PATH_NOT_FOUND,
    resolve_exec_sandbox_roots,
    resolve_exec_tool_cwd,
    resolve_read_tool_path,
    resolve_write_tool_path,
)
from pantaray_agents.local_runtime.tooling.models import ToolDefinitionSeed
from pantaray_agents.local_runtime.tooling.repository.tool_definitions import (
    seed_tool_definitions,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    update_read_access_scope,
)

from .broker_test_support import BROKER_ACTOR_PROCESS_ID
from .path_access_policy_support import (
    bootstrap_path_policy_runtime_db,
    build_manifest_root,
    build_path_policy_context,
)

_USER_ID = "user-1"
_ACTION_ID = "action-1"
_NOW = "2026-03-23T00:00:00Z"


@pytest.mark.asyncio
async def test_unmanaged_db_tool_is_rejected_as_unsupported_broker_tool(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("unmanaged_tool",),
    )
    seed_tool_definitions(
        db_path=db_path,
        busy_timeout_ms=1_000,
        definitions=(
            ToolDefinitionSeed(
                tool_id="unmanaged_tool",
                tool_name="Unmanaged Tool",
                tool_description="DB-only tool",
                category="test",
                risk_level="low",
                intent_class="read_only",
                required_capabilities=("scoped_read",),
                input_schema_json={"type": "object", "properties": {}},
                output_schema_json={"type": "object", "properties": {}},
                rate_limit_json=None,
                default_timeout_ms=None,
                llm_guide_json={},
                is_enabled=True,
                version="test",
            ),
        ),
    )

    with pytest.raises(
        BrokerPolicyError, match="unsupported broker tool: unmanaged_tool"
    ):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="unmanaged_tool",
            user_id=_USER_ID,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={},
        )


def test_broker_registry_declares_path_access_kind_for_all_managed_tools() -> None:
    validate_broker_registry()

    assert {
        tool_id: definition.path_access_kind
        for tool_id, definition in BROKER_TOOL_REGISTRY.definitions.items()
    } == {
        "read": "read",
        "render_pdf_page": "read",
        "list": "read",
        "glob": "read",
        "grep": "read",
        "apply_patch": "write",
        "bash": "exec",
        "run_python": "exec",
    }


def test_resolvers_enforce_registry_path_access_kind(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(BrokerPolicyError, match="not a read/search tool"):
        resolve_read_tool_path(
            context=cast(
                BrokerContext,
                build_path_policy_context(cwd_path=workspace, path_access_kind="write"),
            ),
            raw_path="notes.txt",
            must_exist=False,
        )
    with pytest.raises(BrokerPolicyError, match="not a workspace write tool"):
        resolve_write_tool_path(
            context=cast(
                BrokerContext,
                build_path_policy_context(cwd_path=workspace, path_access_kind="read"),
            ),
            raw_path="notes.txt",
            must_exist=False,
        )
    with pytest.raises(BrokerPolicyError, match="not a workspace exec tool"):
        resolve_exec_tool_cwd(
            context=cast(
                BrokerContext,
                build_path_policy_context(cwd_path=workspace, path_access_kind="write"),
            ),
            raw_cwd=".",
        )


def test_missing_path_suggestions_are_scope_safe_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README-secret.md").write_text("secret\n", encoding="utf-8")
    context = cast(
        BrokerContext,
        build_path_policy_context(cwd_path=workspace, path_access_kind="read"),
    )
    yielded = 0

    def bounded_iterdir(self: Path):
        nonlocal yielded
        if self != workspace:
            return original_iterdir(self)
        for index in range(SUGGESTION_SCAN_LIMIT + 50):
            yielded += 1
            yield workspace / f"README-{index}.md"

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", bounded_iterdir)

    with pytest.raises(BrokerPolicyError) as inside_missing:
        resolve_read_tool_path(
            context=context,
            raw_path="READM.md",
            must_exist=True,
        )
    with pytest.raises(BrokerPolicyError) as outside_missing:
        resolve_read_tool_path(
            context=context,
            raw_path=str(outside / "README-typo.md"),
            must_exist=True,
        )

    assert inside_missing.value.code == READ_PATH_NOT_FOUND
    assert yielded == SUGGESTION_SCAN_LIMIT
    assert outside_missing.value.code == READ_SCOPE_DENIED
    assert "README-secret.md" not in str(outside_missing.value)


def test_write_resolver_normalizes_missing_existing_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_write_tool_path(
            context=cast(
                BrokerContext,
                build_path_policy_context(cwd_path=workspace, path_access_kind="write"),
            ),
            raw_path="missing.txt",
            must_exist=True,
        )

    assert exc_info.value.code == WRITE_PATH_NOT_FOUND
    assert "existing workspace path" in str(exc_info.value)
    assert (
        "Retry with an existing path under workspace roots" == exc_info.value.fix_hint
    )


def test_write_resolver_normalizes_capability_denial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    blocked = tmp_path / "blocked"
    for path in (workspace, blocked):
        path.mkdir()
    context = cast(
        BrokerContext,
        build_path_policy_context(
            cwd_path=workspace,
            path_access_kind="write",
            manifest_roots=(
                build_manifest_root(path=workspace, name="workspace"),
                build_manifest_root(
                    path=blocked,
                    name="blocked",
                    can_apply_patch=False,
                ),
            ),
        ),
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_write_tool_path(
            context=context,
            raw_path=str(blocked / "created.txt"),
            must_exist=False,
        )

    assert exc_info.value.code == WRITE_PATH_DENIED
    assert (
        "Use a path under Workspace Roots that is writable by apply_patch."
        == exc_info.value.fix_hint
    )


def test_exec_resolver_normalizes_missing_cwd(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_exec_tool_cwd(
            context=cast(
                BrokerContext,
                build_path_policy_context(cwd_path=workspace, path_access_kind="exec"),
            ),
            raw_cwd="missing-dir",
        )

    assert exc_info.value.code == EXEC_CWD_NOT_FOUND
    assert "existing workspace directory" in str(exc_info.value)
    assert (
        "Retry with `.` or an existing directory under workspace roots."
        == exc_info.value.fix_hint
    )


def test_exec_resolver_normalizes_capability_denial(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    blocked = tmp_path / "blocked"
    for path in (workspace, blocked):
        path.mkdir()
    context = cast(
        BrokerContext,
        build_path_policy_context(
            cwd_path=workspace,
            path_access_kind="exec",
            manifest_roots=(
                build_manifest_root(path=workspace, name="workspace"),
                build_manifest_root(
                    path=blocked,
                    name="blocked",
                    can_process_write=False,
                ),
            ),
        ),
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        resolve_exec_tool_cwd(context=context, raw_cwd=str(blocked))

    assert exc_info.value.code == EXEC_CWD_DENIED
    assert (
        "Use `.` or a command cwd under Workspace Roots with command execution access."
        == exc_info.value.fix_hint
    )


def test_exec_sandbox_roots_apply_process_read_capability(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    repo = tmp_path / "repo"
    read_blocked = tmp_path / "read-blocked"
    temp_dir = scratch / ".runtime-temp"
    for path in (scratch, repo, read_blocked, temp_dir):
        path.mkdir(parents=True)
    context = cast(
        BrokerContext,
        build_path_policy_context(
            cwd_path=scratch,
            path_access_kind="exec",
            manifest_roots=(
                build_manifest_root(path=scratch, name="scratch"),
                build_manifest_root(path=repo, name="repo"),
                build_manifest_root(
                    path=read_blocked,
                    name="read-blocked",
                    can_process_read=False,
                ),
            ),
        ),
    )

    roots = resolve_exec_sandbox_roots(
        context=context,
        action_temp_dir=temp_dir,
    )

    assert roots.read_roots == (
        scratch.resolve(),
        repo.resolve(),
        temp_dir.resolve(),
    )
    assert read_blocked.resolve() not in roots.read_roots
    assert roots.write_roots == (
        scratch.resolve(),
        repo.resolve(),
        read_blocked.resolve(),
    )


@pytest.mark.asyncio
async def test_workspace_read_scope_limits_read_list_glob_and_grep_to_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("read", "list", "glob", "grep"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("needle\n", encoding="utf-8")

    for tool_id, args in (
        ("read", {"path": str(outside_file)}),
        ("list", {"path": str(outside), "max_depth": 1, "limit": 10}),
        ("glob", {"base_path": str(outside), "pattern": "*.txt", "limit": 10}),
        (
            "grep",
            {
                "base_path": str(outside),
                "pattern": "needle",
                "include_glob": "*.txt",
                "max_matches": 10,
            },
        ),
    ):
        with pytest.raises(BrokerPolicyError) as exc_info:
            await execute_broker_tool(
                db_path=db_path,
                busy_timeout_ms=1_000,
                tool_id=tool_id,
                user_id=_USER_ID,
                actor_process_id=BROKER_ACTOR_PROCESS_ID,
                manifest_id=context.manifest_id,
                execution_session_id=context.execution_session_id,
                args=args,
            )
        assert exc_info.value.code == READ_SCOPE_DENIED


@pytest.mark.asyncio
async def test_full_access_read_scope_allows_read_list_glob_and_grep_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("read", "list", "glob", "grep"),
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("needle\n", encoding="utf-8")

    read_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="read",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": str(outside_file)},
    )
    list_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": str(outside), "max_depth": 1, "limit": 10},
    )
    glob_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": str(outside), "pattern": "*.txt", "limit": 10},
    )
    grep_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="grep",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={
            "base_path": str(outside),
            "pattern": "needle",
            "include_glob": "*.txt",
            "max_matches": 10,
        },
    )

    assert read_outcome.output["path"] == str(outside_file)
    assert [entry["path"] for entry in list_outcome.output["entries"]] == [
        str(outside_file)
    ]
    assert [match["path"] for match in glob_outcome.output["matches"]] == [
        str(outside_file)
    ]
    assert [
        (match["path"], match["line"]) for match in grep_outcome.output["matches"]
    ] == [(str(outside_file), "needle")]


@pytest.mark.asyncio
async def test_read_access_scope_is_snapshotted_by_execution_session(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("read",),
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
        now=_NOW,
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="read",
            user_id=_USER_ID,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={"path": str(outside)},
        )

    assert exc_info.value.code == READ_SCOPE_DENIED


@pytest.mark.asyncio
async def test_full_access_snapshot_survives_later_workspace_setting(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("read",),
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        now="2026-03-23T00:00:01Z",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="read",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": str(outside)},
    )

    assert outcome.output["path"] == str(outside)


@pytest.mark.asyncio
async def test_full_access_read_scope_does_not_expand_write_or_exec_scope(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("apply_patch", "bash"),
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    # Paths outside the workspace still wait for the user's approval.
    with pytest.raises(BrokerApprovalRequiredError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id=_USER_ID,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-patch-outside",
            preflight_only=True,
            args={
                "changes": [
                    {
                        "op": "add",
                        "path": str(outside / "created.txt"),
                        "new_lines": ["outside"],
                        "trailing_newline": True,
                    }
                ]
            },
        )

    with pytest.raises(BrokerApprovalRequiredError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id=_USER_ID,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-outside",
            preflight_only=True,
            args={"command": "pwd", "cwd": str(outside)},
        )
