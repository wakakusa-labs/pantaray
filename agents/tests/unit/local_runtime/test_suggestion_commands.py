from __future__ import annotations

import re
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.tooling.suggestion_research.commands as commands
from pantaray_agents.agents.suggestion_agent.react import (
    SUGGESTION_COMMAND_TOOL_ID,
    SUGGESTION_INTEGRATION_TOOL_IDS,
    SUGGESTION_TOOL_IDS,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolOutput,
    ToolError,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_preference_setting,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    BrokerToSandboxCommandRequest,
)
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    render_seatbelt_profile,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    LocalSuggestionResearchTools,
    discard_suggestion_tool_results,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.commands import (
    SuggestionCommandSession,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.runtime import (
    suggestion_tool_results_root,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)

from .test_suggestion_research_tools import (
    BUSY_TIMEOUT_MS,
    _register_workspace,
    _snapshot,
)
from .test_workspace_settings_repository import TIMESTAMP, _bootstrap_db


def _set_command_approval(
    db_path: Path, *, mode: str, capabilities: tuple[str, ...]
) -> None:
    # What the settings route writes for the workspace edit and command scope.
    preference_id = "preference-workspace-edit-and-command"
    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=ApprovalPreferenceUpsertInput(
            preference_id=preference_id,
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode=mode,  # type: ignore[arg-type]
            applies_to=("workspace_edit_and_command",),
            created_at=TIMESTAMP,
            updated_at=TIMESTAMP,
        ),
        grants=tuple(
            CapabilityGrantCreateInput(
                grant_id=f"grant-{capability}",
                user_id="user-1",
                preference_id=preference_id,
                capability=capability,
                scope_type="global",
                scope_ref=None,
                grant_source="settings",
                granted_at=TIMESTAMP,
            )
            for capability in capabilities
        ),
        revoked_at=TIMESTAMP,
    )


def _call(args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=SUGGESTION_COMMAND_TOOL_ID,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id=SUGGESTION_COMMAND_TOOL_ID,
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )


def _command(command: str, *, cwd: str | None = None, login: bool = False):
    return _call({"command": command, "cwd": cwd, "use_login_environment": login})


def _session(db_path: Path, root: Path, *, scope: str = "workspace"):
    session = SuggestionCommandSession(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        workspace_roots=(root.resolve(),),
        read_access_scope=scope,  # type: ignore[arg-type]
        spill_root=suggestion_tool_results_root(db_path=db_path, run_id="run-1"),
    )
    # Building the tools, as a run does when it starts, creates its folder.
    session.definitions()
    return session


@pytest.fixture
def allowed_db(tmp_path: Path) -> Path:
    db_path = _bootstrap_db(tmp_path)
    _set_command_approval(
        db_path,
        mode="always_allow",
        capabilities=("scoped_write", "process_exec_local"),
    )
    return db_path


@pytest.mark.parametrize(
    ("mode", "capabilities", "offered"),
    [
        ("always_allow", ("scoped_write", "process_exec_local"), True),
        ("prompt_each_time", (), False),
        # The broker does not auto-approve a saved always_allow without its grants.
        ("always_allow", (), False),
        # Nothing saved: the built-in always_allow default needs no grants.
        (None, (), True),
    ],
)
async def test_commands_are_offered_only_while_they_run_without_asking(
    tmp_path: Path, mode: str | None, capabilities: tuple[str, ...], offered: bool
) -> None:
    db_path = _bootstrap_db(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    _register_workspace(db_path=db_path, root=root)
    if mode is not None:
        _set_command_approval(db_path, mode=mode, capabilities=capabilities)

    definitions = await LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")

    assert tuple(definition.name for definition in definitions) == tuple(
        tool_id
        for tool_id in SUGGESTION_TOOL_IDS
        # No integration is connected here.
        if tool_id not in SUGGESTION_INTEGRATION_TOOL_IDS
        and (offered or tool_id != SUGGESTION_COMMAND_TOOL_ID)
    )
    for definition in definitions:
        if definition.name == SUGGESTION_COMMAND_TOOL_ID:
            # Only an Action's bash can ask to run outside the sandbox.
            assert definition.request_schema["additionalProperties"] is False
            assert "run_outside_sandbox" not in definition.request_schema["properties"]


@pytest.mark.asyncio
async def test_a_command_after_consent_is_withdrawn_does_not_run(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(allowed_db, tmp_path)
    _set_command_approval(allowed_db, mode="prompt_each_time", capabilities=())

    async def must_not_run(**_kwargs: object) -> None:
        raise AssertionError("a command ran without consent")

    monkeypatch.setattr(commands, "run_unrecorded_sandbox_command", must_not_run)
    result = await session.run(_command("git status"), 1)

    assert result.status == "error"
    assert result.output["error_code"] == "COMMANDS_NOT_ALLOWED"


def _capture_request(
    monkeypatch: pytest.MonkeyPatch, temp_dir: Path, output: BashToolOutput
) -> list[BrokerToSandboxCommandRequest]:
    requests: list[BrokerToSandboxCommandRequest] = []

    async def fake_run(*, db_path, build_request):  # type: ignore[no-untyped-def]
        requests.append(build_request(temp_dir))
        return ("exited", output)

    monkeypatch.setattr(commands, "run_unrecorded_sandbox_command", fake_run)
    return requests


_EXITED = BashToolOutput(status="success", exit_code=0, stdout="", stderr="")


@pytest.mark.parametrize("network_enabled", [True, False])
@pytest.mark.asyncio
async def test_a_command_gets_no_writable_folder_and_the_network_setting(
    allowed_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_enabled: bool,
) -> None:
    temp_dir = tmp_path / "temp"
    requests = _capture_request(monkeypatch, temp_dir, _EXITED)
    update_command_network_enabled(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        command_network_enabled=network_enabled,
        now=TIMESTAMP,
    )

    await _session(allowed_db, tmp_path).run(_command("git log -1"), 1)

    (request,) = requests
    assert request.run_outside_sandbox is False
    assert request.real_write_roots == []
    assert request.action_storage is None
    assert request.cwd == str(temp_dir)
    assert request.network_policy == ("allow" if network_enabled else "deny")
    profile = render_seatbelt_profile(request)
    # Only the template's leading deny-all may lack a filter; another one after
    # the allows would deny that operation everywhere.
    for operation in ("read", "write"):
        assert len(re.findall(rf"\(deny file-{operation}\*\s*\)", profile)) == 1
    write_allow = profile.split("(allow file-write*", 1)[1].split(")\n", 1)[0]
    assert write_allow.strip() == '(literal "/dev/null"'
    # The database's folder stays unreadable even though "/" is not requested;
    # only the command's own temp dir inside it is excepted.
    assert (
        "(deny file-read* (require-all (require-any\n"
        f'    (subpath "{allowed_db.parent.resolve()}")'
    ) in profile
    assert f'(require-not (require-any\n    (subpath "{temp_dir}")\n))))' in profile


@pytest.mark.parametrize(
    ("login", "scope", "whole_disk"),
    [
        (True, "full_access", True),
        (False, "full_access", True),
        (True, "workspace", False),
        (False, "workspace", False),
    ],
)
@pytest.mark.asyncio
async def test_command_reads_follow_the_read_access_setting(
    allowed_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: bool,
    scope: str,
    whole_disk: bool,
) -> None:
    requests = _capture_request(monkeypatch, tmp_path / "temp", _EXITED)

    await _session(allowed_db, tmp_path, scope=scope).run(
        _command("gh pr view 1", login=login), 1
    )

    assert ("/" in requests[0].real_read_roots) is whole_disk


@pytest.mark.asyncio
async def test_a_cwd_outside_the_workspace_folders_is_refused(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    requests = _capture_request(monkeypatch, tmp_path / "temp", _EXITED)

    refused = await _session(allowed_db, root).run(_command("ls", cwd=str(tmp_path)), 1)
    accepted = await _session(allowed_db, root).run(_command("ls", cwd=str(root)), 1)

    assert refused.output["error_code"] == "CWD_DENIED"
    assert accepted.status == "success"
    assert [request.cwd for request in requests] == [str(root.resolve())]


def _read_args(reference: dict[str, object]) -> dict[str, object]:
    hint = str(reference["retry_hint"])
    return {
        "path": hint.split("path=")[1].split(" ")[0],
        "offset": int(hint.split("offset=")[1].split(",")[0]),
    }


@pytest.mark.asyncio
async def test_long_output_is_saved_where_the_runs_read_reads_it_on(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _register_workspace(db_path=allowed_db, root=root)
    limit = ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    stdout = "\n".join(f"diff line {number:05d}" for number in range(limit // 5))
    stderr = "".join(f"warning {number:05d}\n" for number in range(limit // 10))
    stderr += "fatal: the error at the end\n"
    _capture_request(
        monkeypatch,
        tmp_path / "temp",
        BashToolOutput(
            status="error",
            exit_code=1,
            stdout=stdout,
            stderr=stderr,
            error=ToolError(type="CommandExecutionError", message=stderr),
        ),
    )
    definitions = await LocalSuggestionResearchTools(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=allowed_db),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    registry = ReactToolRegistry(definitions)

    result = await registry.execute(_command("git diff"), 1)

    # A nonzero exit is an answer the model reads, not a failed call.
    assert result.status == "success"
    output = result.output
    assert output["exit_code"] == 1
    assert len(serialize_json_tool_output(output)) <= limit
    # The error is printed last, and the preview keeps the end of each stream.
    assert output["stderr"]["preview"].endswith("fatal: the error at the end\n")
    assert output["stdout"]["preview"].startswith("diff line 00000\n")
    spill_root = suggestion_tool_results_root(db_path=allowed_db, run_id="suggestion-1")
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        reference = output[name]
        assert Path(reference["path"]).is_relative_to(spill_root.resolve())
        args = _read_args(reference)
        read = await registry.execute(
            ReactToolCall(
                tool_name="read",
                tool_args=args,  # type: ignore[arg-type]
                tool_call_envelope=ToolCallEnvelope(
                    tool_id="read",
                    reason=None,
                    args=args,  # type: ignore[arg-type]
                ),
            ),
            2,
        )
        assert read.status == "success", read.output
        # The lines the preview left out are where read goes on.
        first_left_out = text.splitlines()[int(args["offset"]) - 1]
        assert first_left_out not in reference["preview"]
        assert read.output["content"].startswith(first_left_out)

    discard_suggestion_tool_results(db_path=allowed_db, run_id="suggestion-1")
    assert not spill_root.exists()


@pytest.mark.asyncio
async def test_a_command_that_did_not_finish_is_an_error_with_its_output_saved(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = "\n".join(f"build step {number:05d}" for number in range(5_000))

    async def timed_out(*, db_path, build_request):  # type: ignore[no-untyped-def]
        return (
            "timed_out",
            BashToolOutput(
                status="error",
                exit_code=-1,
                stdout=partial,
                stderr="",
                error=ToolError(type="ToolTimeoutError", message="timed out"),
            ),
        )

    monkeypatch.setattr(commands, "run_unrecorded_sandbox_command", timed_out)
    registry = ReactToolRegistry(_session(allowed_db, tmp_path).definitions())
    result = await registry.execute(_command("make"), 1)

    assert result.status == "error"
    assert result.output["error_code"] == "ToolTimeoutError"
    stdout = result.output["details"]["stdout"]
    assert stdout["preview"].endswith("build step 04999")
    assert Path(stdout["path"]).read_text(encoding="utf-8") == partial
