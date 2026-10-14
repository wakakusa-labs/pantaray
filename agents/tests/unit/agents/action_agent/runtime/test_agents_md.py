from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.unit.agents.action_agent.runtime.test_checkout_status import (
    _clone_pair,
    _commit,
    _git,
)
from tests.unit.local_runtime.broker_test_support import BROKER_ACTOR_PROCESS_ID
from tests.unit.local_runtime.path_access_policy_support import (
    ACTION_ID,
    USER_ID,
    bootstrap_path_policy_runtime_db,
)

from pantaray_agents.agents.action_agent.runtime import agents_md
from pantaray_agents.agents.action_agent.runtime import (
    checkout_status as checkout_status_module,
)
from pantaray_agents.agents.action_agent.runtime.agents_md import (
    AGENTS_MD_MAX_BYTES,
    RepositoryContextClaim,
    claim_repository_context,
    load_pantaray_agents_md,
    unclaimed_checkouts,
)
from pantaray_agents.agents.action_agent.runtime.checkout_status import (
    checkout_status,
)
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.builders import (
    _build_tool_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.broker_tools import (
    run_broker_tool_wrapper,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.support.conversation_projection import (
    project_action_conversation,
)
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    READ_TOOL_ID,
    BrokerContext,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs import (
    action_subagent_broker as subagent_broker,
)
from pantaray_agents.utils.trace_context import TraceContextManager


class _Action:
    """One Action's broker identity, state and read context over a real DB."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        read_access_scope: str = READ_ACCESS_SCOPE_FULL_ACCESS,
    ) -> None:
        self.db_path, context = bootstrap_path_policy_runtime_db(
            tmp_path,
            allowed_tool_ids=("read", "list", "apply_patch", "bash"),
            read_access_scope=read_access_scope,
        )
        monkeypatch.setenv("LOCAL_DB_PATH", str(self.db_path))
        monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
        execution = cast(Any, context)
        self.workspace: Path = execution.workspace_path
        self.state = create_initial_state(
            user_id=USER_ID,
            suggestion_id=None,
            action_id=ACTION_ID,
            started_at="2026-10-01T00:00:00Z",
            max_steps=10,
            max_tool_steps=10,
            token_budget=None,
            manifest_id=execution.manifest_id,
            execution_session_id=execution.execution_session_id,
            execution_network_policy=execution.network_policy,
            action_temp_dir=str(execution.action_temp_dir),
            app_runtime_python=str(execution.app_runtime_python),
            read_access_scope=cast(Any, read_access_scope),
        )

    def read_context(self) -> BrokerContext:
        return load_broker_context(
            db_path=self.db_path,
            busy_timeout_ms=1_000,
            tool_id=READ_TOOL_ID,
            path_access_kind="read",
            user_id=USER_ID,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=str(self.state["manifest_id"]),
            execution_session_id=str(self.state["execution_session_id"]),
        )

    def attach(self, tool_id: str, args: dict[str, JSONValue]) -> str | None:
        context = self.state["context"]
        attached = list(context.get("agents_md_attached_paths", []))
        read_context = self.read_context()
        # The status a live call reads off the event loop, read here in line.
        statuses = {
            checkout: status
            for checkout in unclaimed_checkouts(
                attached, tool_id=tool_id, args=args, read_context=read_context
            )
            if (status := checkout_status(checkout)) is not None
        }
        text = claim_repository_context(
            attached,
            tool_id=tool_id,
            args=args,
            read_context=read_context,
            statuses=statuses,
        ).text
        context["agents_md_attached_paths"] = attached
        return text

    async def read(self, path: Path | str, **extra: JSONValue) -> Any:
        with TraceContextManager(extra={"process_id": BROKER_ACTOR_PROCESS_ID}):
            preparation = await run_broker_tool_wrapper(
                None,
                "step-1",
                cast(ToolDefinition, _ReadTool()),
                {"path": str(path), **extra},
                self.state,
                invocation_id=None,
                tool_request_id=f"request-{os.urandom(4).hex()}",
                requested_at="2026-10-01T00:00:00Z",
                preflight_only=False,
            )
        return preparation.result

    def resume(self) -> None:
        self.state = restore_runtime_state_checkpoint(
            build_runtime_state_checkpoint(self.state),
            expected_action_id=ACTION_ID,
            expected_suggestion_id=None,
            expected_user_id=USER_ID,
        )


class _ReadTool:
    tool_id = READ_TOOL_ID
    name = "read"


def _repository(root: Path) -> Path:
    """``root/repo`` is a git repository with a root and a nested AGENTS.md."""

    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    (repo / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    (repo / "sub" / "AGENTS.md").write_text("SUB RULE\n", encoding="utf-8")
    (repo / "sub" / "x.py").write_text("print(1)\n", encoding="utf-8")
    # Above the repository root: never part of it.
    (root / "AGENTS.md").write_text("ABOVE ROOT\n", encoding="utf-8")
    return repo.resolve()


def _block(directory: Path, text: str) -> str:
    return (
        f"# AGENTS.md instructions for {directory}\n\n<INSTRUCTIONS>\n{text}\n"
        "</INSTRUCTIONS>"
    )


@pytest.mark.asyncio
async def test_repository_files_attach_once_from_the_git_root_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path)

    first = await action.read(repo / "sub" / "x.py")

    assert first.status == "success"
    assert first.agents_md == (
        _block(repo, "ROOT RULE\n") + "\n\n" + _block(repo / "sub", "SUB RULE\n")
    )
    assert (await action.read(repo / "sub" / "x.py")).agents_md is None
    assert action.attach("list", {"path": str(repo)}) is None
    action.resume()
    assert (await action.read(repo / "sub" / "x.py")).agents_md is None
    assert action.attach("bash", {"command": "ls", "cwd": str(repo / "sub")}) is None


@pytest.mark.asyncio
async def test_a_failed_call_keeps_its_error_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path)

    failed = await action.read(repo / "sub" / "missing.py")

    assert failed.status == "error"
    assert failed.agents_md is None
    assert failed.output["error"]["code"] == "READ_PATH_NOT_FOUND"
    assert "agents_md_attached_paths" not in action.state["context"]
    assert (await action.read(repo / "sub" / "x.py")).agents_md is not None


def test_each_touching_tool_attaches_by_the_directory_it_works_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path / "a")
    other = _repository(tmp_path / "b")
    action = _Action(tmp_path, monkeypatch)

    # Without a cwd the command runs in the Action's scratch directory.
    assert action.attach("bash", {"command": "ls"}) is None
    patched = action.attach(
        "apply_patch",
        {"changes": [{"op": "delete", "path": str(repo / "sub" / "gone.py")}]},
    )
    assert patched is not None and "SUB RULE" in patched
    assert action.attach("read", {"path": str(other / "AGENTS.md")}) == _block(
        other, "ROOT RULE\n"
    )
    listed = action.attach("list", {"path": str(other / "sub")})
    assert listed == _block(other / "sub", "SUB RULE\n")


@pytest.mark.asyncio
async def test_a_partial_read_of_agents_md_still_keeps_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path)
    (repo / "AGENTS.md").write_text("FIRST LINE\nSECOND RULE\n", encoding="utf-8")

    partial = await action.read(repo / "AGENTS.md", limit=1)

    assert partial.status == "success"
    assert "SECOND RULE" not in str(partial.output)
    assert partial.agents_md == _block(repo, "FIRST LINE\nSECOND RULE\n")
    action.state["history_by_scope"]["S"].append(
        _build_tool_history_entry(
            step_id="tool-1",
            step_number=1,
            phase="executing",
            summary="note",
            tool_id="read",
            started_at="2026-10-01T00:00:00Z",
            completed_at="2026-10-01T00:00:01Z",
            result_line="read: ok",
            args={"path": str(repo / "AGENTS.md"), "limit": 1},
            output=partial.output,
            short_step_id="S-1-TOOL",
            agents_md=partial.agents_md,
        )
    )
    action.resume()
    rendered = ActionAgentFormatter().format_history(
        action.state, omit_before_step_number=2
    )
    assert "SECOND RULE" in rendered
    assert (await action.read(repo / "AGENTS.md")).agents_md is None


def test_files_the_read_tool_may_not_open_are_not_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    # tmp_path is the repository, so private app storage lies inside it.
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    private = action.db_path.parent / "AGENTS.md"
    private.write_text("PRIVATE\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("OUTSIDE\n", encoding="utf-8")
    project = tmp_path / "project"
    (project / "escape").mkdir(parents=True)
    (project / "AGENTS.md").symlink_to(private)
    (project / "escape" / "AGENTS.md").symlink_to(outside / "AGENTS.md")

    attached = action.attach("list", {"path": str(project / "escape")})

    assert attached == _block(tmp_path.resolve(), "ROOT RULE\n")


def test_an_in_repository_symlink_is_attached_for_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path)
    (repo / "sub" / "CLAUDE.md").write_text("SHARED RULE\n", encoding="utf-8")
    (repo / "sub" / "AGENTS.md").unlink()
    (repo / "sub" / "AGENTS.md").symlink_to("CLAUDE.md")

    attached = action.attach("list", {"path": str(repo / "sub")})

    assert attached == (
        _block(repo, "ROOT RULE\n") + "\n\n" + _block(repo / "sub", "SHARED RULE\n")
    )


def test_a_symlink_swapped_in_after_validation_reads_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path / "work")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE SECRET\n", encoding="utf-8")
    validate = agents_md.resolve_read_tool_path

    def validate_then_swap(**kwargs: Any) -> Any:
        resolved = validate(**kwargs)
        if kwargs.get("must_be_file") and resolved.path == repo / "sub" / "AGENTS.md":
            resolved.path.unlink()
            resolved.path.symlink_to(outside)
        return resolved

    monkeypatch.setattr(agents_md, "resolve_read_tool_path", validate_then_swap)

    attached = action.attach("list", {"path": str(repo / "sub")})

    assert attached == _block(repo, "ROOT RULE\n")


def test_a_parent_swapped_for_a_symlink_after_validation_reads_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(
        tmp_path, monkeypatch, read_access_scope=READ_ACCESS_SCOPE_WORKSPACE
    )
    packages = action.workspace / "packages"
    repo = _repository(packages)
    outside = tmp_path / "outside"
    (outside / "repo" / "sub").mkdir(parents=True)
    (outside / "repo" / "sub" / "AGENTS.md").write_text(
        "OUTSIDE SECRET\n", encoding="utf-8"
    )
    validate = agents_md.resolve_read_tool_path

    def validate_then_swap_parent(**kwargs: Any) -> Any:
        resolved = validate(**kwargs)
        if kwargs.get("must_be_file") and resolved.path == repo / "sub" / "AGENTS.md":
            packages.rename(action.workspace / "packages-moved")
            packages.symlink_to(outside, target_is_directory=True)
        return resolved

    monkeypatch.setattr(agents_md, "resolve_read_tool_path", validate_then_swap_parent)

    attached = action.attach("list", {"path": str(repo / "sub")})

    assert attached == _block(repo, "ROOT RULE\n")


def test_workspace_scope_never_looks_above_the_registered_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(
        tmp_path, monkeypatch, read_access_scope=READ_ACCESS_SCOPE_WORKSPACE
    )
    parent = action.workspace.parent
    (parent / ".git").mkdir()
    (parent / "AGENTS.md").write_text("ABOVE THE ROOT\n", encoding="utf-8")
    (action.workspace / "AGENTS.md").write_text("WORKSPACE RULE\n", encoding="utf-8")
    (action.workspace / "notes.txt").write_text("n\n", encoding="utf-8")

    attached = action.attach("read", {"path": str(action.workspace / "notes.txt")})

    assert attached == _block(action.workspace.resolve(), "WORKSPACE RULE\n")


def test_odd_files_are_skipped_decoded_or_truncated_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    repo = _repository(tmp_path)
    (repo / "AGENTS.md").write_bytes(b"R" * (AGENTS_MD_MAX_BYTES + 100))
    (repo / "sub" / "AGENTS.md").write_bytes(b"caf\xff rule\n")
    (repo / "blank").mkdir()
    (repo / "blank" / "AGENTS.md").write_text(" \n\t\n", encoding="utf-8")
    (repo / "fifo").mkdir()
    os.mkfifo(repo / "fifo" / "AGENTS.md")

    first = action.attach("list", {"path": str(repo / "sub")})

    # The root file spends the whole batch budget; the nested one waits.
    assert first == _block(repo, "R" * AGENTS_MD_MAX_BYTES)
    assert action.attach("list", {"path": str(repo / "sub")}) == _block(
        repo / "sub", "caf� rule\n"
    )
    assert action.attach("list", {"path": str(repo / "blank")}) is None
    assert action.attach("list", {"path": str(repo / "fifo")}) is None

    home = tmp_path / "home"
    (home / ".pantaray").mkdir(parents=True)
    os.mkfifo(home / ".pantaray" / "AGENTS.md")
    monkeypatch.setenv("HOME", str(home))
    assert load_pantaray_agents_md() == ""


def test_the_pantaray_wide_file_is_wrapped_and_absent_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_pantaray_agents_md() == ""
    assert not (tmp_path / ".pantaray").exists()

    (tmp_path / ".pantaray").mkdir()
    (tmp_path / ".pantaray" / "AGENTS.md").write_text("Be brief.\n", encoding="utf-8")

    assert load_pantaray_agents_md() == _block(Path("~/.pantaray"), "Be brief.\n")


def test_attached_instructions_survive_output_omission() -> None:
    entry: dict[str, Any] = {
        "step_id": "tool-2",
        "step_number": 2,
        "phase": "executing",
        "step_type": StepType.TOOL_EXECUTION,
        "summary": "note",
        "tool_id": "read",
        "started_at": "2026-10-01T00:00:00Z",
        "completed_at": "2026-10-01T00:00:01Z",
        "short_step_id": "S-2-TOOL",
        "args": {"path": "/repo/x.py"},
        "output": {"content": "BODY"},
        "result_line": "read: ok",
        "call_id": "call_a",
        "llm_step_id": "think-1",
        "agents_md": "RULES",
    }
    think: dict[str, Any] = {
        **entry,
        "step_id": "think-1",
        "step_number": 1,
        "step_type": StepType.LLM_OUTPUT,
        "tool_id": None,
        "short_step_id": "S-1-THINK",
    }
    for key in ("args", "output", "result_line", "call_id", "llm_step_id"):
        think.pop(key)
    think.pop("agents_md")
    projection = project_action_conversation(
        cast(Any, [think, entry]),
        request_fingerprint="",
        omit_before_step_number=3,
        turn_context="TC",
        repair_notice="",
        provider_turns={},
    )
    assert projection is not None
    result = cast(Any, projection.conversation[1])
    assert result.output["agents_md"] == "RULES"
    assert "BODY" not in str(result.output)

    state = cast(ActionAgentState, {"history_by_scope": {"S": [think, entry]}})
    rendered = ActionAgentFormatter().format_history(state, omit_before_step_number=3)
    assert "RULES" in rendered
    assert "BODY" not in rendered


def _behind_checkout(root: Path) -> Path:
    """A checkout with a file to read, one commit behind origin/main."""

    root.mkdir(parents=True)
    work, other = _clone_pair(root)
    _commit(other, "landed")
    _git(other, "push")
    _git(work, "fetch")
    (work / "x.py").write_text("print(1)\n", encoding="utf-8")
    return work.resolve()


@pytest.mark.asyncio
async def test_the_first_call_in_a_checkout_says_how_far_it_is_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    work = _behind_checkout(tmp_path / "behind")
    level, _other = _clone_pair(tmp_path)
    checked: list[Path] = []
    real_describe = checkout_status_module._describe
    monkeypatch.setattr(
        checkout_status_module,
        "_describe",
        lambda path: checked.append(Path(path)) or real_describe(path),
    )

    first = await action.read(work / "x.py")

    assert first.status == "success"
    assert first.agents_md == (
        f"Git checkout {work} (branch main) is 1 commits behind origin/main, "
        "as of the last fetch (merge commits not counted); "
        "`git fetch` updates these counts."
    )
    # Once per checkout per Action, a resumed run included.
    assert (await action.read(work / "x.py")).agents_md is None
    action.resume()
    assert action.attach("bash", {"command": "ls", "cwd": str(work)}) is None
    # A level checkout gets nothing, and is not read again either.
    assert action.attach("list", {"path": str(level)}) is None
    assert action.attach("list", {"path": str(level)}) is None
    assert checked == [work, level.resolve()]


@pytest.mark.asyncio
async def test_a_checkout_that_cannot_be_read_leaves_the_call_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _Action(tmp_path, monkeypatch)
    work = _behind_checkout(tmp_path / "behind")

    def _broken(_path: str) -> str:
        raise LookupError("a codec git output could not be read with")

    monkeypatch.setattr(checkout_status_module, "_describe", _broken)

    result = await action.read(work / "x.py")

    assert result.status == "success"
    assert result.agents_md is None


async def _ticks_while(work: Awaitable[Any]) -> tuple[Any, int]:
    """Run ``work`` and count how often the event loop got to run meanwhile."""

    ticks = 0
    done = asyncio.Event()

    async def tick() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        return await work, ticks
    finally:
        done.set()
        await ticker


def _slow_git(monkeypatch: pytest.MonkeyPatch, *, seconds: float) -> None:
    real_describe = checkout_status_module._describe

    def slow(path: str) -> str | None:
        time.sleep(seconds)
        return real_describe(path)

    monkeypatch.setattr(checkout_status_module, "_describe", slow)


@pytest.mark.asyncio
async def test_a_slow_git_never_holds_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling calls and the Stop watch keep running while a checkout is read,
    and a read past the time limit only drops the sentence."""

    action = _Action(tmp_path, monkeypatch)
    work = _behind_checkout(tmp_path / "behind")
    _slow_git(monkeypatch, seconds=0.5)

    result, ticks = await _ticks_while(action.read(work / "x.py"))

    assert result.agents_md is not None and "1 commits behind" in result.agents_md
    assert ticks >= 10

    other = _behind_checkout(tmp_path / "other")
    monkeypatch.setattr(checkout_status_module, "_STATUS_TIMEOUT_SECONDS", 0.1)
    result, ticks = await _ticks_while(action.read(other / "x.py"))

    assert result.status == "success"
    assert result.agents_md is None
    assert ticks >= 2


@pytest.mark.asyncio
async def test_a_slow_git_never_holds_a_subagents_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _behind_checkout(tmp_path / "behind")
    _slow_git(monkeypatch, seconds=0.5)
    monkeypatch.setattr(
        subagent_broker, "load_broker_context", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        subagent_broker, "unclaimed_checkouts", lambda _attached, **_kwargs: (work,)
    )
    monkeypatch.setattr(
        subagent_broker,
        "claim_repository_context",
        lambda _attached, *, statuses, **_kwargs: RepositoryContextClaim(
            keys=("checkout",), text=statuses.get(work)
        ),
    )
    recorded: list[str | None] = []
    claims = SimpleNamespace(
        attached=[], record=lambda _paths, text: recorded.append(text)
    )

    _, ticks = await _ticks_while(
        subagent_broker._claim_agents_md(
            cast(Any, claims),
            db_path=tmp_path / "unused.sqlite3",
            busy_timeout_ms=1_000,
            payload=cast(Any, {"user_id": "u", "process_id": "p"}),
            authority=cast(
                Any, SimpleNamespace(manifest_id="m", execution_session_id="s")
            ),
            tool_id="read",
            args={"path": str(work / "x.py")},
        )
    )

    assert ticks >= 10
    assert recorded and recorded[0] is not None and "1 commits behind" in recorded[0]


@pytest.mark.asyncio
async def test_a_run_saved_while_a_status_is_read_still_owes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling saving the run mid-read stores no claim for what is not yet
    sent, so a run resumed from that save still gets the status and AGENTS.md."""

    action = _Action(tmp_path, monkeypatch)
    work = _behind_checkout(tmp_path / "behind")
    (work / "AGENTS.md").write_text("RULE\n", encoding="utf-8")
    _slow_git(monkeypatch, seconds=0.3)

    reading = asyncio.create_task(action.read(work / "x.py"))
    await asyncio.sleep(0.1)
    saved = build_runtime_state_checkpoint(action.state)
    assert (await reading).agents_md is not None

    action.state = restore_runtime_state_checkpoint(
        saved,
        expected_action_id=ACTION_ID,
        expected_suggestion_id=None,
        expected_user_id=USER_ID,
    )
    resumed = await action.read(work / "x.py")

    assert resumed.agents_md is not None
    assert "RULE" in resumed.agents_md
    assert "1 commits behind origin/main" in resumed.agents_md
