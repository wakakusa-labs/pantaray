from __future__ import annotations

import re
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.tooling.suggestion_research.commands as commands
from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.agents.suggestion_agent.react import (
    SUGGESTION_COMMAND_TOOL_ID,
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
)
from pantaray_agents.local_runtime.tooling.suggestion_research.commands import (
    SuggestionCommandSession,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
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
    return SuggestionCommandSession(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        workspace_roots=(root.resolve(),),
        read_access_scope=scope,  # type: ignore[arg-type]
    )


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
        # The broker does not auto-approve always_allow without its grants.
        ("always_allow", (), False),
    ],
)
def test_commands_are_offered_only_while_they_run_without_asking(
    tmp_path: Path, mode: str, capabilities: tuple[str, ...], offered: bool
) -> None:
    db_path = _bootstrap_db(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    _register_workspace(db_path=db_path, root=root)
    _set_command_approval(db_path, mode=mode, capabilities=capabilities)

    definitions = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    ).build_tool_definitions(user_id="user-1", run_id="suggestion-1")

    assert tuple(definition.name for definition in definitions) == tuple(
        tool_id
        for tool_id in SUGGESTION_TOOL_IDS
        if offered or tool_id != SUGGESTION_COMMAND_TOOL_ID
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


@pytest.mark.asyncio
async def test_long_output_is_cut_to_what_an_action_keeps_inline(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    stderr = "e" * limit + "fatal: the error at the end"
    _capture_request(
        monkeypatch,
        tmp_path / "temp",
        BashToolOutput(
            status="error",
            exit_code=1,
            stdout="o" * (limit * 2),
            stderr=stderr,
            error=ToolError(type="CommandExecutionError", message=stderr),
        ),
    )

    result = await _session(allowed_db, tmp_path).run(_command("git diff"), 1)

    # A nonzero exit is an answer the model reads, not a failed call.
    assert result.status == "success"
    assert result.output["exit_code"] == 1
    assert result.output["truncated"] is True
    assert len(result.output["stdout"]) + len(result.output["stderr"]) == limit
    # The error is printed last, so stderr keeps its end.
    assert result.output["stderr"].endswith("fatal: the error at the end")
    shown_stdout = len(result.output["stdout"])
    shown_stderr = len(result.output["stderr"])
    assert result.output["notes"] == [
        f"stdout was cut: showing the first {shown_stdout:,} of {limit * 2:,} "
        "characters. Narrow it with grep, head, tail or sed -n to read the rest.",
        f"stderr was cut: showing the last {shown_stderr:,} of {len(stderr):,} "
        "characters.",
    ]


@pytest.mark.asyncio
async def test_a_command_that_did_not_finish_is_a_bounded_error(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timed_out(*, db_path, build_request):  # type: ignore[no-untyped-def]
        return (
            "timed_out",
            BashToolOutput(
                status="error",
                exit_code=-1,
                stdout="partial",
                stderr="",
                error=ToolError(type="ToolTimeoutError", message="timed out"),
            ),
        )

    monkeypatch.setattr(commands, "run_unrecorded_sandbox_command", timed_out)
    result = await _session(allowed_db, tmp_path).run(_command("sleep 999"), 1)

    assert result.status == "error"
    assert result.output["error_code"] == "ToolTimeoutError"
    assert result.output["details"]["stdout"] == "partial"
