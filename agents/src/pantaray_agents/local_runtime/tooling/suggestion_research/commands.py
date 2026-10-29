"""Shell commands for a Suggestion run.

A Suggestion has nobody to ask for approval, so it runs commands only while the
user's default lets Actions run them without asking. Each command runs in the
Action's command sandbox with the Action's read scope and limits. A research
command has no writable folder at all and follows the user's network setting;
a make-stage command writes only into the Suggestion's artifact folder and its
own temp dir, with the network off. The make stage's ``run_python`` is such a
command: the app runtime Python, run on its program as the Action's is.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from pantaray_agents.agents.suggestion_agent.react import (
    SUGGESTION_COMMAND_TOOL_ID,
    SUGGESTION_PYTHON_TOOL_ID,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    ReadAccessScope,
)
from pantaray_agents.tools.contract import (
    JsonSchema,
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
from ..brokering.broker_protocol import BashToolOutput, BrokerExecutionKind
from ..brokering.broker_registry import BROKER_TOOL_REGISTRY
from ..brokering.command_runtime import build_command_env
from ..outside_workspace_grant import app_owned_roots
from ..repository import (
    load_active_capability_grants,
    load_effective_approval_preference,
)
from ..repository.command_invocation_audits import TerminalOutcome
from ..repository.command_network_settings import load_command_network_enabled
from ..sandbox.command_sandbox_protocol import (
    ActionSandboxStorage,
    BrokerToSandboxCommandRequest,
)
from ..sandbox.command_sandbox_request import (
    GENERATED_PYTHON_SCRIPT_NAME,
    compose_sandbox_request,
)
from ..sandbox.runtime_policy import SandboxLaunchBudget, resolve_runtime_budget
from ..sandbox.unrecorded_command import run_unrecorded_sandbox_command
from ..tool_result_spill import accepting_spilled_texts
from ..tool_result_storage import store_run_tool_result

BASH_EXECUTABLE = Path("/bin/bash")
# The error message stays a short string; stdout and stderr carry the output.
# The same bound submit_suggestion puts on rejection feedback.
ERROR_MESSAGE_MAX_CHARS = 1_200
_SPILL_ROOT_MODE = 0o700
# What storage stores whole when it cannot move stdout or stderr out alone.
_WHOLE_RESULT_STORED: dict[str, JSONValue] = {"required": ["storage", "path"]}


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


def _react_result(
    *, tool_name: str, terminal_outcome: TerminalOutcome, output: BashToolOutput
) -> ReactToolResult:
    streams: dict[str, JSONValue] = {
        "exit_code": output.exit_code,
        "stdout": output.stdout,
        "stderr": output.stderr,
    }
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


def _python_budget() -> SandboxLaunchBudget:
    return resolve_runtime_budget(
        sandbox_profile="agent_generated_python"
    ).sandbox_launch


@dataclass(frozen=True, slots=True)
class PinnedFolder:
    """A real directory, known by its identity from the moment it was pinned.

    A command runs in it only while that same directory is still at ``path``:
    a later call is never led elsewhere by whatever the path became.
    """

    path: Path
    device: int
    inode: int

    @classmethod
    def pin(cls, path: Path) -> PinnedFolder:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):  # lstat: a symlink is no directory
            raise NotADirectoryError(f"not a real directory: {path}")
        return cls(path, info.st_dev, info.st_ino)

    def unchanged(self) -> bool:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return False
        return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
            self.device,
            self.inode,
        )


@dataclass(frozen=True, slots=True)
class SuggestionCommandSession:
    db_path: Path
    busy_timeout_ms: int
    user_id: str
    workspace_roots: tuple[Path, ...]
    read_access_scope: ReadAccessScope
    # The run's folder for output too long to show inline, created when the
    # tools are built at the start of the run.
    spill_root: Path
    # The make stage's artifact folder: the only place it writes.
    artifact_folder: PinnedFolder | None = None

    def _description(self) -> str:
        if self.artifact_folder is None:
            purpose = (
                "Run one non-interactive shell command to check current state, "
                "for example `git log` or `gh pr view`. No file can be written; "
                "the network follows the user's command network setting. Never "
                "run a command that changes something elsewhere, such as "
                "merging, pushing, sending or deleting."
            )
        else:
            purpose = (
                "Run one non-interactive shell command to make or check the "
                "Suggestion's files. Files can be written only in its artifact "
                f"folder, `{self.artifact_folder.path}`, where a null cwd starts; "
                "workspace folders are read-only and the network is off."
            )
        return (
            f"{purpose} When stdout or stderr is too long to show inline, it is "
            "saved to a file instead and gives its path, a preview of its start "
            "and end, and how to read the rest with read or grep."
        )

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        self.spill_root.mkdir(mode=_SPILL_ROOT_MODE, parents=True, exist_ok=True)
        definitions = (
            ReactToolDefinition(
                name=SUGGESTION_COMMAND_TOOL_ID,
                description=self._description(),
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
                                "workspace folder, or null for "
                                + (
                                    "an empty temporary directory."
                                    if self.artifact_folder is None
                                    else "the artifact folder."
                                )
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
                        "anyOf": [
                            accepting_spilled_texts(
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "status",
                                        "exit_code",
                                        "stdout",
                                        "stderr",
                                    ],
                                    "properties": {
                                        "status": {"const": "success"},
                                        "exit_code": {"type": "integer"},
                                        "stdout": {"type": "string"},
                                        "stderr": {"type": "string"},
                                    },
                                }
                            ),
                            _WHOLE_RESULT_STORED,
                        ]
                    }
                ),
                execute=self.run,
            ),
        )
        if self.artifact_folder is None:
            return definitions
        return (*definitions, self._python_definition(definitions[0].response_schema))

    def _python_definition(self, response_schema: JsonSchema) -> ReactToolDefinition:
        assert self.artifact_folder is not None
        return ReactToolDefinition(
            name=SUGGESTION_PYTHON_TOOL_ID,
            description=(
                "Run one short Python 3.12 program to make or check the "
                "Suggestion's files, such as a Word, Excel or PowerPoint file, an "
                "image or a PDF. Besides the standard library it can import "
                "python-docx (docx, .docx), openpyxl (.xlsx), python-pptx (pptx, "
                ".pptx), Pillow (PIL, images), pypdf (read, merge or split PDFs), "
                "numpy and lxml; nothing else can be installed, and there is no "
                "plotting library, so draw a chart with Pillow, or as SVG or "
                "HTML. Files can be written only in the artifact folder, "
                f"`{self.artifact_folder.path}`, where a null cwd starts, and in "
                "the temp dir tempfile gives; workspace folders are read-only and "
                "the network is off. A program runs at most "
                f"{_python_budget().timeout_ms // 1000} seconds. Look at a file "
                "you made with render_pdf_page. Long stdout or stderr is saved to "
                "a file and read back with read or grep."
            ),
            request_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "cwd"],
                "properties": {
                    "code": {"type": "string", "minLength": 1, "pattern": r"\S"},
                    "cwd": {
                        "type": ["string", "null"],
                        "description": (
                            "Absolute path of a directory inside a listed "
                            "workspace folder, or null for the artifact folder."
                        ),
                    },
                },
            },
            response_schema=response_schema,
            execute=self.run_python,
        )

    def _resolve_cwd(self, raw_cwd: JSONValue) -> tuple[Path, Path] | None:
        """(cwd, the workspace folder holding it), or None for the temp dir."""

        if raw_cwd is None:
            folder = self.artifact_folder
            return None if folder is None else (folder.path, folder.path)
        if not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute():
            raise ValueError("cwd must be an absolute path or null")
        cwd = Path(raw_cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {raw_cwd}")
        for root in (*self.workspace_roots, *self._artifact_roots()):
            if cwd.is_relative_to(root):
                return cwd, root
        raise ValueError(f"cwd is outside the listed workspace folders: {raw_cwd}")

    def _artifact_roots(self) -> tuple[Path, ...]:
        return () if self.artifact_folder is None else (self.artifact_folder.path,)

    async def run(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = call.tool_args
        assert isinstance(args, dict)  # Validated against request_schema.
        command = args["command"]
        use_login_environment = args["use_login_environment"]
        assert isinstance(command, str) and isinstance(use_login_environment, bool)
        return await self._run_sandboxed(
            call,
            raw_cwd=args.get("cwd"),
            bash_command=command,
            use_login_environment=use_login_environment,
        )

    async def run_python(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = call.tool_args
        assert isinstance(args, dict)  # Validated against request_schema.
        code = args["code"]
        assert isinstance(code, str)
        return await self._run_sandboxed(
            call, raw_cwd=args.get("cwd"), python_code=code, use_login_environment=False
        )

    async def _run_sandboxed(
        self,
        call: ReactToolCall,
        *,
        raw_cwd: JSONValue,
        use_login_environment: bool,
        bash_command: str | None = None,
        python_code: str | None = None,
    ) -> ReactToolResult:
        """Run bash on ``bash_command``, or the app runtime Python on
        ``python_code`` as the Action's run_python runs it."""

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
        if self.artifact_folder is not None and not self.artifact_folder.unchanged():
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="ARTIFACT_FOLDER_REPLACED",
                message="The artifact folder was replaced, so no command can run.",
            )
        try:
            resolved_cwd = self._resolve_cwd(raw_cwd)
        except (ValueError, OSError) as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="CWD_DENIED",
                message=str(exc),
            )
        network_enabled = self.artifact_folder is None and load_command_network_enabled(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            user_id=self.user_id,
        )
        full_disk_read = self.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
        app_runtime_python = load_and_verify_app_runtime_python_from_env()
        if python_code is None:
            assert bash_command is not None
            executable = BASH_EXECUTABLE
            arguments = ["--noprofile", "--norc", "-c", bash_command]
            execution_kind: BrokerExecutionKind = "workspace_command"
            budget = resolve_runtime_budget(
                sandbox_profile="workspace_process_exec"
            ).sandbox_launch
        else:
            executable = app_runtime_python
            arguments = []
            execution_kind = "agent_generated"
            budget = _python_budget()
        private_storage_roots = [str(root) for root in app_owned_roots(self.db_path)]

        def build_request(temp_dir: Path) -> BrokerToSandboxCommandRequest:
            cwd, execution_root = resolved_cwd or (temp_dir, temp_dir)
            argv = [str(executable), *arguments]
            if python_code is not None:
                # Where the Action's run_python puts its program: the temp dir.
                script_path = temp_dir / GENERATED_PYTHON_SCRIPT_NAME
                script_path.write_text(python_code, encoding="utf-8")
                argv.append(str(script_path))
            return compose_sandbox_request(
                temp_dir=temp_dir,
                real_read_roots=[
                    *(["/"] if full_disk_read else []),
                    *(str(root) for root in self.workspace_roots),
                    *(str(root) for root in self._artifact_roots()),
                ],
                real_write_roots=[]
                if self.artifact_folder is None
                else [str(self.artifact_folder.path), str(temp_dir)],
                private_storage_roots=private_storage_roots,
                # The artifact folder sits in private storage, whose deny it lifts.
                action_storage=None
                if self.artifact_folder is None
                else ActionSandboxStorage(
                    workspace_root=str(self.artifact_folder.path),
                    published_results_root=str(self.artifact_folder.path),
                ),
                app_runtime_python=app_runtime_python,
                cwd=str(cwd),
                argv=argv,
                env=build_command_env(
                    command_cwd=cwd,
                    execution_kind=execution_kind,
                    workspace_root=execution_root,
                    resolved_executable=executable,
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
        result = _react_result(
            tool_name=call.tool_name, terminal_outcome=terminal_outcome, output=output
        )
        return replace(
            result,
            output=store_run_tool_result(
                spill_root=self.spill_root, output=result.output
            ),
        )


__all__ = ["PinnedFolder", "SuggestionCommandSession", "commands_run_without_asking"]
