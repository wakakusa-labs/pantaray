"""Read-only shell commands for a Suggestion run.

A Suggestion has nobody to ask for approval, so it runs commands only while the
user's default lets Actions run them without asking. Each command runs in the
Action's command sandbox with the Action's read scope, network setting and
limits, but with no writable folder at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.suggestion_agent.react import SUGGESTION_COMMAND_TOOL_ID
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    ReadAccessScope,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)

from ...app_runtime_verification import load_and_verify_app_runtime_python_from_env
from ..brokering.broker_common import (
    APPROVAL_MODE_ALWAYS_ALLOW,
    APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
)
from ..brokering.broker_protocol import BashToolOutput
from ..brokering.broker_registry import BROKER_TOOL_REGISTRY
from ..brokering.command_runtime import build_command_env
from ..outside_workspace_grant import app_owned_roots
from ..repository import (
    load_active_capability_grants,
    load_effective_approval_preference,
)
from ..repository.command_invocation_audits import TerminalOutcome
from ..repository.command_network_settings import load_command_network_enabled
from ..sandbox.command_sandbox_protocol import BrokerToSandboxCommandRequest
from ..sandbox.command_sandbox_request import compose_sandbox_request
from ..sandbox.runtime_policy import resolve_runtime_budget
from ..sandbox.unrecorded_command import run_unrecorded_sandbox_command
from ..tool_result_storage import ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT

BASH_EXECUTABLE = Path("/bin/bash")
# An Action keeps a result this long inline in its transcript. A Suggestion has
# no spill file to page through, so it keeps that much and says what it cut.
STDERR_MAX_CHARS = ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT // 4
# The same bound submit_suggestion puts on rejection feedback.
ERROR_MESSAGE_MAX_CHARS = 1_200


def commands_run_without_asking(
    *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> bool:
    """Whether the user's default lets Actions run commands without asking.

    The broker's rule for that default: a saved always_allow counts only together
    with the durable capability grants the setting writes with it; the unsaved
    built-in default needs none.
    """

    preference = load_effective_approval_preference(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        applies_to=APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
    )
    if preference.approval_mode != APPROVAL_MODE_ALWAYS_ALLOW:
        return False
    if preference.preference_id is None:
        return True
    grants = load_active_capability_grants(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        applies_to=APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
    )
    required = BROKER_TOOL_REGISTRY.definitions["bash"].required_capabilities
    return all(capability in grants for capability in required)


def _bounded_streams(output: BashToolOutput) -> dict[str, JSONValue]:
    # A command prints its error last, so stderr keeps its end.
    stderr = output.stderr[max(0, len(output.stderr) - STDERR_MAX_CHARS) :]
    stdout = output.stdout[: ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT - len(stderr)]
    notes: list[JSONValue] = []
    if len(stdout) < len(output.stdout):
        notes.append(
            f"stdout was cut: showing the first {len(stdout):,} of "
            f"{len(output.stdout):,} characters. Narrow it with grep, head, tail "
            "or sed -n to read the rest."
        )
    if len(stderr) < len(output.stderr):
        notes.append(
            f"stderr was cut: showing the last {len(stderr):,} of "
            f"{len(output.stderr):,} characters."
        )
    return {
        "exit_code": output.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(notes),
        "notes": notes,
    }


def _react_result(
    *, tool_name: str, terminal_outcome: TerminalOutcome, output: BashToolOutput
) -> ReactToolResult:
    streams = _bounded_streams(output)
    if terminal_outcome == "exited":
        # A nonzero exit status is an answer (no match, a failed check), not a
        # failed tool call.
        return ReactToolResult(
            tool_name=tool_name,
            status="success",
            output={"status": "success", **streams},
        )
    assert output.error is not None  # Every other outcome carries its error.
    return tool_error_response(
        tool_name=tool_name,
        error_code=output.error.type,
        message=output.error.message[:ERROR_MESSAGE_MAX_CHARS],
        details=streams,
    )


@dataclass(frozen=True, slots=True)
class SuggestionCommandSession:
    db_path: Path
    busy_timeout_ms: int
    user_id: str
    workspace_roots: tuple[Path, ...]
    read_access_scope: ReadAccessScope

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return (
            ReactToolDefinition(
                name=SUGGESTION_COMMAND_TOOL_ID,
                description=(
                    "Run one non-interactive shell command to check current state, "
                    "for example `git log` or `gh pr view`. No file can be written; "
                    "the network follows the user's command network setting. Never "
                    "run a command that changes something elsewhere, such as "
                    "merging, pushing, sending or deleting. Output is limited to "
                    f"{ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT:,} characters: "
                    "stdout keeps its beginning and stderr its last "
                    f"{STDERR_MAX_CHARS:,} characters. When either is cut, "
                    "truncated is true and notes say how much; narrow the output "
                    "with grep, head, tail or sed -n."
                ),
                request_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command", "cwd", "use_login_environment"],
                    "properties": {
                        "command": {"type": "string", "minLength": 1, "pattern": r"\S"},
                        "cwd": {
                            "type": ["string", "null"],
                            "description": (
                                "Absolute path of a directory inside a listed "
                                "workspace folder, or null for an empty temporary "
                                "directory."
                            ),
                        },
                        "use_login_environment": {
                            "type": "boolean",
                            "description": (
                                "true to use the user's sign-ins (real HOME, "
                                "Keychain, ssh-agent), for CLIs such as gh."
                            ),
                        },
                    },
                },
                response_schema=react_tool_response_schema(
                    success_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "status",
                            "exit_code",
                            "stdout",
                            "stderr",
                            "truncated",
                            "notes",
                        ],
                        "properties": {
                            "status": {"const": "success"},
                            "exit_code": {"type": "integer"},
                            "stdout": {"type": "string"},
                            "stderr": {"type": "string"},
                            "truncated": {"type": "boolean"},
                            "notes": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                ),
                execute=self.run,
            ),
        )

    def _resolve_cwd(self, raw_cwd: JSONValue) -> tuple[Path, Path] | None:
        """(cwd, the workspace folder holding it), or None for the temp dir."""

        if raw_cwd is None:
            return None
        if not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute():
            raise ValueError("cwd must be an absolute path or null")
        cwd = Path(raw_cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {raw_cwd}")
        for root in self.workspace_roots:
            if cwd.is_relative_to(root):
                return cwd, root
        raise ValueError(f"cwd is outside the listed workspace folders: {raw_cwd}")

    async def run(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = call.tool_args
        assert isinstance(args, dict)  # Validated against request_schema.
        command = args["command"]
        use_login_environment = args["use_login_environment"]
        assert isinstance(command, str) and isinstance(use_login_environment, bool)
        # Read per call, as the broker does: a change the user makes while the
        # run is in progress applies to the next command.
        if not commands_run_without_asking(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            user_id=self.user_id,
        ):
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="COMMANDS_NOT_ALLOWED",
                message="Commands now need the user's approval, so none can run.",
            )
        try:
            resolved_cwd = self._resolve_cwd(args.get("cwd"))
        except (ValueError, OSError) as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="CWD_DENIED",
                message=str(exc),
            )
        network_enabled = load_command_network_enabled(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            user_id=self.user_id,
        )
        full_disk_read = self.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
        budget = resolve_runtime_budget(
            sandbox_profile="workspace_process_exec", db_path=self.db_path
        ).sandbox_launch
        app_runtime_python = load_and_verify_app_runtime_python_from_env()
        private_storage_roots = [str(root) for root in app_owned_roots(self.db_path)]

        def build_request(temp_dir: Path) -> BrokerToSandboxCommandRequest:
            cwd, execution_root = resolved_cwd or (temp_dir, temp_dir)
            return compose_sandbox_request(
                temp_dir=temp_dir,
                real_read_roots=[
                    *(["/"] if full_disk_read else []),
                    *(str(root) for root in self.workspace_roots),
                ],
                real_write_roots=[],
                private_storage_roots=private_storage_roots,
                action_storage=None,
                app_runtime_python=app_runtime_python,
                cwd=str(cwd),
                argv=[str(BASH_EXECUTABLE), "--noprofile", "--norc", "-c", command],
                env=build_command_env(
                    command_cwd=cwd,
                    execution_kind="workspace_command",
                    workspace_root=execution_root,
                    resolved_executable=BASH_EXECUTABLE,
                    use_login_environment=use_login_environment,
                    full_disk_read=full_disk_read,
                ),
                timeout_ms=budget.timeout_ms,
                stdout_max_bytes=budget.stdout_max_bytes,
                stderr_max_bytes=budget.stderr_max_bytes,
                temp_storage_limit_bytes=budget.temp_storage_limit_bytes,
                network_policy="allow" if network_enabled else "deny",
                use_login_environment=use_login_environment,
                run_outside_sandbox=False,
            )

        terminal_outcome, output = await run_unrecorded_sandbox_command(
            db_path=self.db_path, build_request=build_request
        )
        return _react_result(
            tool_name=call.tool_name, terminal_outcome=terminal_outcome, output=output
        )


__all__ = ["SuggestionCommandSession", "commands_run_without_asking"]
