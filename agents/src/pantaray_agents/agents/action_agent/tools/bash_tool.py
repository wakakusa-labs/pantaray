"""Brokered workspace bash tool definition."""

from __future__ import annotations

from dataclasses import replace

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolArgs,
    SandboxedBashToolArgs,
)
from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    build_validation_input_schema,
    tool_execution_policy,
)
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

# When and how to ask for access beyond the workspace defaults is in the access
# fields' descriptions, which the model reads with the schema.
_ACCESS_REQUEST_GUIDE = (
    "When a call important to the task needs to write outside the workspace, "
    "rerun the same call with additional_write_folders. Do not ask the user in "
    "chat first, and do not work around it with other tools or locations."
)

# Shared with run_python, whose calls ask for outside write folders the same way.
WRITE_FOLDER_REQUEST_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="additional_write_folders",
        description=(
            "Folders outside the workspace this call must write. Set it only after "
            "a call failed because it could not write there (for example 'Operation "
            "not permitted' or 'Read-only file system'), or when the command writes "
            "there on every run (for example a CLI that stores its state under the "
            "home folder, like codex exec); never just in case, and never for reads. "
            "Absolute paths, or paths starting with ~/ for the user's real home; only "
            "the folders needed, each the most specific existing folder. Pantaray's "
            "own storage and folders that contain it (/, the home folder, ~/Library) "
            "cannot be requested. The call waits for the user's approval and, once "
            "approved, can write only there besides the workspace."
        ),
    ),
    BrokerToolFieldPresentation(
        name="justification",
        description=(
            "Give justification whenever you set use_login_environment, "
            "additional_write_folders, or run_outside_sandbox, and only then. It is "
            "shown to the user, as written, as the approval question: one short "
            "sentence in the language of the user's request, for someone who does "
            "not read commands. With use_login_environment, say which service's "
            "signed-in account it will use and for what, naming only the service "
            "the command actually uses (for a Japanese request, for example: "
            "「GitHub にログイン済みのアカウントで、PR の状態を確認します。」). "
            "With additional_write_folders, say what allowing the change lets you "
            "do. With run_outside_sandbox, say in one or two sentences what the "
            "command will do and what it produces (for example: "
            "「ブラウザで資料のページを開き、PDF に書き出します。」). Do not "
            "include command names, paths, or file names."
        ),
    ),
)

BASH_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="command",
        description=(
            "Non-interactive shell command or script. "
            "Pipes, redirections, heredocs, environment assignments, "
            "and multiple commands are supported."
        ),
    ),
    BrokerToolFieldPresentation(
        name="cwd",
        description=(
            "Set cwd to a workspace path.\n"
            "- Use the current workspace marker, an absolute local path "
            "under Workspace Roots in Workspace Path Rules, or a "
            "subdirectory under a registered workspace.\n"
            "- A cwd outside registered workspaces waits for the user to "
            "approve that one call; use one only when the user asked for "
            "that location.\n"
            "- Command path arguments are relative to cwd."
        ),
    ),
    BrokerToolFieldPresentation(
        name="use_login_environment",
        description=(
            "Default false. Set true to run with the user's login environment "
            "(real HOME, Keychain, ssh-agent) so CLIs the user signed in to in "
            "their terminal work.\n"
            "- Use it when the command needs the user's sign-in or personal "
            "settings (for example gh, git commit/push or cloning a private "
            "repository, cloud CLIs), or when a normal run failed with an "
            "authentication error.\n"
            "- Writes stay limited to workspace folders and approved folders; "
            "keep clone and output paths there."
        ),
    ),
    BrokerToolFieldPresentation(
        name="run_outside_sandbox",
        description=(
            "Default false. Set true only to rerun a command important to the "
            "task that failed because of the sandbox, when "
            "additional_write_folders and use_login_environment cannot fix it: "
            "for example macOS denied a system service or launching an app (a "
            "headless browser that aborts at startup), or 'Operation not "
            "permitted' on something other than writing a folder. Never set it "
            "pre-emptively, for convenience, or before trying the command "
            "normally.\n"
            "- The call waits for the user to approve this exact command once, "
            "whatever the approval mode, then runs it with the user's own "
            "permissions; if the user does not allow it, find another way.\n"
            "- Keep the command to the step that needs it. Do not combine it "
            "with additional_write_folders."
        ),
    ),
    *WRITE_FOLDER_REQUEST_FIELD_PRESENTATION,
)

_BASH_INPUT_DESCRIPTION = "Run a non-interactive workspace shell command or script."

BASH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="bash",
        name="Bash",
        description=(
            "Run a non-interactive shell command or script in a workspace. "
            "Set cwd to the working directory; path arguments are relative to cwd."
        ),
        guide=ToolGuideSpec(
            what=(
                "Run workspace scripts, virtual-environment tools, tests, builds, "
                "and package commands with the installed shell and toolchains. "
                "Pipes, redirections, heredocs, and multiple commands are supported."
            ),
            when=(
                "Direct file content edits should normally go through apply_patch; "
                "commands that write files as their normal side effect are still "
                "command execution. Use list, glob, grep, or read for file discovery "
                "and inspection. For short waits, use sleep 10 or sleep 0.5, then "
                "check status in a separate call. Read Pantaray's own records "
                "(suggestions, actions, insights, activity logs) with memory_sql, "
                "not by opening the app database. " + _ACCESS_REQUEST_GUIDE
            ),
            pitfalls=(
                "Set cwd to a directory under Workspace Roots, or outside them only "
                "when the user asked for that location; '.' uses the current "
                "execution workspace and path arguments are relative to cwd. "
                "Use $TMPDIR for temporary files. TMPDIR, and HOME unless "
                "use_login_environment is set, are temporary and removed after "
                "this call. Commands and descendants follow the "
                "user's command network setting and workspace file permissions. "
                "This call waits for completion and has a finite execution timeout. "
                "Do not use interactive prompts, sudo, or background processes "
                "intended to survive this call. Shell startup files are not loaded."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="process_exec_local",
            required_capabilities=("process_exec_local",),
            default_timeout_ms=60_000,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=BashToolArgs,
            fields=BASH_TOOL_FIELD_PRESENTATION,
            description=_BASH_INPUT_DESCRIPTION,
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
            "required": ["status", "exit_code", "stdout", "stderr"],
            "additionalProperties": False,
        },
    )
)

# A subagent writes only what it claimed, which an unsandboxed command cannot be
# held to, so its bash does not offer run_outside_sandbox (the broker refuses it).
ACTION_SUBAGENT_BASH_TOOL = replace(
    BASH_TOOL,
    input_schema=build_validation_input_schema(
        broker_tool_input_spec_from_model(
            model=SandboxedBashToolArgs,
            fields=tuple(
                field
                for field in BASH_TOOL_FIELD_PRESENTATION
                if field.name != "run_outside_sandbox"
            ),
            description=_BASH_INPUT_DESCRIPTION,
        )
    ),
)
