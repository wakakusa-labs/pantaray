from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _bootstrap_runtime_db_with_registered_folder,
)


@pytest.mark.asyncio
async def test_execute_broker_tool_lists_workspace_directory(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    src_dir = context.workspace_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (context.workspace_path / "README.md").write_text("# Notes\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": ".", "max_depth": 6, "limit": 10},
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["warning"] is None
    entries = outcome.output["entries"]
    assert isinstance(entries, list)
    assert {entry["path"] for entry in entries} >= {
        str(context.workspace_path / "README.md"),
        str(context.workspace_path / "src"),
        str(context.workspace_path / "src" / "app.py"),
    }


@pytest.mark.asyncio
async def test_list_dot_returns_current_execution_workspace(tmp_path: Path) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    (context.workspace_path / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    (repo / "repo.txt").write_text("repo\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": ".", "max_depth": 1, "limit": 10},
    )

    assert outcome.status == "success"
    entries = outcome.output["entries"]
    assert isinstance(entries, list)
    assert {entry["path"] for entry in entries} >= {
        str(context.workspace_path / "scratch.txt"),
    }
    assert str(repo / "repo.txt") not in {entry["path"] for entry in entries}


@pytest.mark.asyncio
async def test_list_full_access_allows_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "external.txt"
    outside_file.write_text("outside\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": str(outside_dir), "max_depth": 1, "limit": 10},
    )

    assert outcome.status == "success"
    assert {entry["path"] for entry in outcome.output["entries"]} == {str(outside_file)}


@pytest.mark.asyncio
async def test_list_workspace_scope_missing_inside_path_returns_not_found(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="list",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={"path": str(context.workspace_path / "missing"), "limit": 10},
            invocation_id="inv-list-missing-inside",
            tool_request_id="req-list-missing-inside",
        )

    assert exc_info.value.code == "READ_PATH_NOT_FOUND"
    assert "workspace roots" in str(exc_info.value.fix_hint)


@pytest.mark.asyncio
async def test_list_workspace_scope_missing_outside_path_returns_scope_denied(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="list",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={"path": str(outside / "missing"), "limit": 10},
            invocation_id="inv-list-missing-outside",
            tool_request_id="req-list-missing-outside",
        )

    assert exc_info.value.code == "READ_SCOPE_DENIED"
    assert "enable full_access" in str(exc_info.value.fix_hint)


@pytest.mark.asyncio
async def test_list_full_access_missing_path_uses_local_path_hint(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="list",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={"path": str(tmp_path / "missing"), "limit": 10},
            invocation_id="inv-list-full-missing",
            tool_request_id="req-list-full-missing",
        )

    assert exc_info.value.code == "READ_PATH_NOT_FOUND"
    assert "local path" in str(exc_info.value.fix_hint)
    assert "Workspace Context" not in str(exc_info.value.fix_hint)


@pytest.mark.asyncio
async def test_discovery_relative_paths_resolve_from_execution_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "src").mkdir()
    (context.workspace_path / "src" / "app.py").write_text(
        "needle\n",
        encoding="utf-8",
    )

    glob_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "**/*.py", "limit": 10},
    )
    grep_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="grep",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={
            "base_path": ".",
            "pattern": "needle",
            "include_glob": "**/*.py",
            "max_matches": 10,
        },
    )

    assert glob_outcome.status == "success"
    assert grep_outcome.status == "success"
    assert [match["path"] for match in glob_outcome.output["matches"]] == [
        str(context.workspace_path / "src" / "app.py")
    ]
    assert [match["path"] for match in grep_outcome.output["matches"]] == [
        str(context.workspace_path / "src" / "app.py")
    ]


@pytest.mark.asyncio
async def test_execute_broker_tool_globs_workspace_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    src_dir = context.workspace_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (src_dir / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    (context.workspace_path / "README.md").write_text("# Notes\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "**/*.py", "limit": 10},
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["warning"] is None
    matches = outcome.output["matches"]
    assert isinstance(matches, list)
    assert [match["path"] for match in matches] == [
        str(context.workspace_path / "src" / "app.py"),
        str(context.workspace_path / "src" / "util.py"),
    ]


@pytest.mark.asyncio
async def test_glob_full_access_allows_absolute_base_path_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "external.py"
    outside_file.write_text("print('outside')\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": str(outside_dir), "pattern": "*.py", "limit": 10},
    )

    assert outcome.status == "success"
    assert [match["path"] for match in outcome.output["matches"]] == [str(outside_file)]


@pytest.mark.asyncio
async def test_glob_dot_searches_current_execution_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    (context.workspace_path / "scratch_app.py").write_text("print('scratch')\n")
    (repo / "repo_app.py").write_text("print('repo')\n")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "**/*.py", "limit": 10},
    )

    assert outcome.status == "success"
    assert [match["path"] for match in outcome.output["matches"]] == [
        str(context.workspace_path / "scratch_app.py"),
    ]


@pytest.mark.asyncio
async def test_glob_single_star_does_not_cross_directory_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    src_dir = context.workspace_path / "src"
    nested_dir = src_dir / "nested"
    nested_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("print('direct')\n", encoding="utf-8")
    (nested_dir / "app.py").write_text("print('nested')\n", encoding="utf-8")

    direct_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "src/*.py", "limit": 10},
    )
    recursive_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "src/**/*.py", "limit": 10},
    )

    direct_matches = direct_outcome.output["matches"]
    recursive_matches = recursive_outcome.output["matches"]
    assert isinstance(direct_matches, list)
    assert isinstance(recursive_matches, list)
    assert [match["path"] for match in direct_matches] == [
        str(context.workspace_path / "src" / "app.py")
    ]
    assert [match["path"] for match in recursive_matches] == [
        str(context.workspace_path / "src" / "app.py"),
        str(context.workspace_path / "src" / "nested" / "app.py"),
    ]
