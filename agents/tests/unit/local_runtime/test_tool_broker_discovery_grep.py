from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _bootstrap_runtime_db_with_registered_folder,
)


@pytest.mark.asyncio
async def test_execute_broker_tool_greps_workspace_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    src_dir = context.workspace_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("needle\nother\n", encoding="utf-8")
    (src_dir / "util.py").write_text("other\nneedle again\n", encoding="utf-8")
    (context.workspace_path / "notes.txt").write_text("needle text\n", encoding="utf-8")

    outcome = await execute_broker_tool(
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

    assert outcome.status == "success"
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["retry_hint"] is None
    assert outcome.output["warning"] is None
    matches = outcome.output["matches"]
    assert isinstance(matches, list)
    assert [
        (match["path"], match["line_number"], match["line"]) for match in matches
    ] == [
        (str(context.workspace_path / "src" / "app.py"), 1, "needle"),
        (str(context.workspace_path / "src" / "util.py"), 2, "needle again"),
    ]


@pytest.mark.asyncio
async def test_grep_does_not_expose_action_plan_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope="full_access",
    )
    private_plan = context.workspace_path / "PLAN.MD"
    private_plan.write_text("needle private\n", encoding="utf-8")
    neighbor = context.workspace_path / "visible.md"
    neighbor.write_text("needle visible\n", encoding="utf-8")
    nested_plan = context.workspace_path / "nested" / "plan.md"
    nested_plan.parent.mkdir()
    nested_plan.write_text("needle ordinary nested plan\n", encoding="utf-8")
    # Searched from above app storage, only the Action's own workspace shows.
    parent = db_path.parent.resolve().parent
    case_alias = parent.with_name(parent.name.swapcase())
    grep_base = case_alias if case_alias.exists() else parent

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="grep",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={
            "base_path": str(grep_base),
            "pattern": "needle",
            "include_glob": (
                f"{context.workspace_path.relative_to(parent).as_posix()}/**/*.md"
            ),
            "max_matches": 2,
        },
    )

    matches = outcome.output["matches"]
    assert len(matches) == 2
    matches_by_line = {match["line"]: Path(match["path"]) for match in matches}
    assert matches_by_line["needle ordinary nested plan"].samefile(nested_plan)
    assert matches_by_line["needle visible"].samefile(neighbor)
    assert "needle private" not in (outcome.search_text or "")
    assert outcome.output["truncated"] is False


@pytest.mark.asyncio
async def test_grep_does_not_expose_action_plan_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    private_plan = context.workspace_path / "plan.md"
    private_plan.write_text("needle private\n", encoding="utf-8")
    hardlink = context.workspace_path / "00-ordinary-name.md"
    hardlink.hardlink_to(private_plan)
    neighbor = context.workspace_path / "visible.md"
    neighbor.write_text("needle visible\n", encoding="utf-8")

    outcome = await execute_broker_tool(
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
            "include_glob": "*.md",
            "max_matches": 1,
        },
    )

    assert [(match["path"], match["line"]) for match in outcome.output["matches"]] == [
        (str(neighbor), "needle visible")
    ]


@pytest.mark.asyncio
async def test_grep_full_access_allows_absolute_base_path_outside_workspace(
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
    outside_file = outside_dir / "external.txt"
    outside_file.write_text("needle outside\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="grep",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={
            "base_path": str(outside_dir),
            "pattern": "needle",
            "include_glob": "*.txt",
            "max_matches": 10,
        },
    )

    assert outcome.status == "success"
    assert [(match["path"], match["line"]) for match in outcome.output["matches"]] == [
        (str(outside_file), "needle outside"),
    ]


@pytest.mark.asyncio
async def test_grep_dot_searches_current_execution_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    (context.workspace_path / "scratch.txt").write_text("needle scratch\n")
    (repo / "repo.txt").write_text("needle repo\n")

    outcome = await execute_broker_tool(
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
            "include_glob": "**/*.txt",
            "max_matches": 10,
        },
    )

    assert outcome.status == "success"
    assert [(match["path"], match["line"]) for match in outcome.output["matches"]] == [
        (str(context.workspace_path / "scratch.txt"), "needle scratch"),
    ]


@pytest.mark.asyncio
async def test_grep_reports_skipped_files_as_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text("needle\n", encoding="utf-8")

    from pantaray_agents.local_runtime.tooling.brokering import broker_discovery
    from pantaray_agents.local_runtime.tooling.brokering.broker_discovery_ripgrep import (
        RipgrepGrepMatch,
        RipgrepGrepResult,
    )

    def fake_grep(**_: object) -> RipgrepGrepResult:
        return RipgrepGrepResult(
            matches=(
                RipgrepGrepMatch(
                    relative_path="notes.txt",
                    line_number=1,
                    line="needle",
                    line_truncated=False,
                ),
            ),
            truncated=False,
            truncation_reason=None,
            timed_out=False,
            skipped_files=2,
            first_skip_error="./locked: Permission denied (os error 13)",
            binary_match_paths=tuple(f"blob-{index}.bin" for index in range(12)),
        )

    monkeypatch.setattr(broker_discovery, "run_ripgrep_grep", fake_grep)

    outcome = await execute_broker_tool(
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
            "include_glob": "*.txt",
            "max_matches": 10,
        },
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is False
    assert outcome.output["truncation_reason"] is None
    assert outcome.output["skipped_files"] == 2
    warning = str(outcome.output["warning"])
    assert "2 path(s) could not be read" in warning
    assert "./locked: Permission denied (os error 13)" in warning
    assert "12 binary file(s) also match" in warning
    assert str(context.workspace_path / "blob-9.bin") in warning
    assert str(context.workspace_path / "blob-10.bin") not in warning
    assert "and 2 more." in warning
    assert "read" in str(outcome.output["retry_hint"])


@pytest.mark.asyncio
async def test_grep_truncates_long_matching_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "minified.js").write_text(
        ("x" * 3_000) + " needle " + ("y" * 3_000) + "\n",
        encoding="utf-8",
    )

    outcome = await execute_broker_tool(
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
            "include_glob": "*.js",
            "max_matches": 10,
        },
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "line_length"
    assert "offset=line_number" in str(outcome.output["retry_hint"])
    assert "excerpt around their first match" in str(outcome.output["warning"])
    matches = outcome.output["matches"]
    assert isinstance(matches, list)
    assert len(matches) == 1
    line = matches[0]["line"]
    assert line.startswith("…x") and line.endswith("y…")
    assert " needle " in line


@pytest.mark.asyncio
async def test_grep_match_limit_names_the_limit_and_keeps_line_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "log.txt").write_text(
        "".join(f"needle {index} {'x' * 600}\n" for index in range(3)),
        encoding="utf-8",
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="grep",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "needle", "max_matches": 2},
    )

    assert outcome.status == "success"
    assert len(outcome.output["matches"]) == 2
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "limit"
    warning = str(outcome.output["warning"])
    assert "max_matches=2" in warning
    assert "excerpt around their first match" in warning
    retry_hint = str(outcome.output["retry_hint"])
    assert "Raise max_matches" in retry_hint
    assert "offset=line_number" in retry_hint


@pytest.mark.asyncio
async def test_grep_caps_total_output_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "many.txt").write_text(
        "".join(f"needle {'x' * 1800} {index}\n" for index in range(100)),
        encoding="utf-8",
    )

    outcome = await execute_broker_tool(
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
            "include_glob": "*.txt",
            "max_matches": 100,
        },
    )

    assert outcome.status == "success"
    assert isinstance(outcome.output, dict)
    assert outcome.output["storage"] == "action_file"
    assert outcome.output["media_type"] == "application/json"
    stored_output = json.loads(
        Path(str(outcome.output["path"])).read_text(encoding="utf-8")
    )
    assert stored_output["truncated"] is True
    assert stored_output["truncation_reason"] == "output_bytes"
    assert "Narrow base_path" in str(stored_output["retry_hint"])
    assert "50 KB output limit" in str(stored_output["warning"])
    matches = stored_output["matches"]
    assert isinstance(matches, list)
    assert len(str(stored_output).encode("utf-8")) <= 70_000
    assert len(matches) < 100


@pytest.mark.asyncio
async def test_discovery_tools_do_not_materialize_full_tree_before_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "a.txt").write_text("needle\n", encoding="utf-8")

    def fail_unbounded_walk(self: Path, pattern: str = "*"):
        raise AssertionError(f"unbounded Path traversal was called: {self} {pattern}")

    def fail_materialized_iterdir(self: Path):
        raise AssertionError(f"materialized Path.iterdir traversal was called: {self}")

    monkeypatch.setattr(Path, "rglob", fail_unbounded_walk)
    monkeypatch.setattr(Path, "glob", fail_unbounded_walk)
    monkeypatch.setattr(Path, "iterdir", fail_materialized_iterdir)

    list_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": ".", "max_depth": 1, "limit": 1},
    )
    glob_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"base_path": ".", "pattern": "*.txt", "limit": 1},
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
            "include_glob": "*.txt",
            "max_matches": 1,
        },
    )

    assert list_outcome.status == "success"
    assert glob_outcome.status == "success"
    assert grep_outcome.status == "success"


@pytest.mark.asyncio
async def test_discovery_scan_budget_applies_before_pattern_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (context.workspace_path / "b.txt").write_text("beta\n", encoding="utf-8")

    from pantaray_agents.local_runtime.tooling.brokering import broker_discovery

    monkeypatch.setattr(broker_discovery, "DISCOVERY_MAX_SCANNED_PATHS", 1)

    list_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": ".", "max_depth": 1, "limit": 10},
    )

    assert list_outcome.status == "success"
    assert list_outcome.output["truncated"] is True
    assert list_outcome.output["truncation_reason"] == "scan_budget"
    assert "narrower path" in str(list_outcome.output["retry_hint"])
    assert "scan budget" in str(list_outcome.output["warning"])
    assert len(list_outcome.output["entries"]) == 1


@pytest.mark.asyncio
async def test_list_scan_budget_counts_skipped_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    links_dir = context.workspace_path / "links"
    links_dir.mkdir()
    for index in range(3):
        (links_dir / f"skip-{index}").symlink_to("missing.txt")

    from pantaray_agents.local_runtime.tooling.brokering import broker_discovery

    monkeypatch.setattr(broker_discovery, "DISCOVERY_MAX_SCANNED_PATHS", 1)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": "links", "max_depth": 1, "limit": 10},
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "scan_budget"
    assert "narrower path" in str(outcome.output["retry_hint"])
    assert "scan budget" in str(outcome.output["warning"])
    assert outcome.output["entries"] == []


@pytest.mark.asyncio
async def test_grep_invalid_regex_returns_policy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "bad-regex.txt").write_text("needle\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="grep",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={
                "base_path": ".",
                "pattern": "(",
                "include_glob": "*.txt",
                "max_matches": 10,
            },
        )

    assert exc_info.value.code == "GREP_PATTERN_INVALID"


@pytest.mark.asyncio
async def test_discovery_tools_accept_workspace_absolute_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "a.txt").write_text("a\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="glob",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={
            "base_path": str(context.workspace_path),
            "pattern": "**/*",
            "limit": 10,
        },
    )

    assert outcome.status == "success"
    assert {
        (match["path"], match["kind"], match["name"])
        for match in outcome.output["matches"]
    } >= {(str(context.workspace_path / "a.txt"), "file", "a.txt")}


@pytest.mark.asyncio
async def test_discovery_tools_report_truncation(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "a.txt").write_text("a\n", encoding="utf-8")
    (context.workspace_path / "b.txt").write_text("b\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="list",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"path": ".", "max_depth": 1, "limit": 1},
    )

    assert outcome.status == "success"
    assert outcome.output["truncated"] is True
    assert outcome.output["truncation_reason"] == "limit"
    assert "narrower path" in str(outcome.output["retry_hint"])
    assert "result limit" in str(outcome.output["warning"])
    assert len(outcome.output["entries"]) == 1
