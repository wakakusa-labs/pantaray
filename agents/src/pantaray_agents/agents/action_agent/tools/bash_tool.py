"""Brokered workspace bash tool definition."""

from __future__ import annotations

from dataclasses import replace

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolArgs,
    SandboxedBashToolArgs,
)
from pantaray_agents.schema.agent.base import JSONValue
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
            "cannot be requested. The call needs approval and, once approved, can "
            "write only there besides the workspace."
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
            "and multiple commands are supported. Only one bash call runs per "
            "turn, so put independent read-only commands you need now into one "
            "call, joined with `;`. Run a command that changes something in its "
            "own call, so that a retry after a failure does not repeat it."
        ),
    ),
    BrokerToolFieldPresentation(
        name="cwd",
        description=(
            "Set cwd to a workspace path.\n"
            "- Use the current workspace marker, an absolute local path "
            "under Workspace Roots in Workspace Path Rules, or a "
            "subdirectory under a registered workspace.\n"
            "- A cwd outside registered workspaces needs approval for that "
            "one call; use one only when the user asked for that location.\n"
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
        name="run_in_background",
        description=(
            "Default false. Set true for a command meant to keep running, such "
            "as a dev server or a file watcher, not to wait less for a build or "
            "test. The call returns after a few seconds with a process_id and "
            "the output so far, or with how the command ended if it already "
            "did. Read its later output with read_process_output, and stop it "
            "with stop_process as soon as it is no longer needed; whatever is "
            "still running is stopped when your work ends. With the command "
            "network setting off, localhost is blocked too, so a dev server "
            "cannot be reached unless the network is on. Cannot be combined "
            "with run_outside_sandbox."
        ),
    ),
    BrokerToolFieldPresentation(
        name="run_outside_sandbox",
        description=(
            "Default false. Set true only to rerun a command important to the "
            "task that failed because of the sandbox, when "
            "additional_write_folders and use_login_environment cannot fix it: "
            "for example macOS denied a system service or launching an app, or "
            "'Operation not permitted' on something other than writing a "
            "folder. Programs that need the window server, such as GUI apps and "
            "browsers, always crash inside the sandbox, so set it on their first "
            "run; to look at a page or export it to PDF, use the browser tool "
            "instead of starting a browser. Never set it "
            "pre-emptively for anything else, for convenience, or before trying "
            "the command normally.\n"
            "- Each call needs approval for this exact command, then runs it "
            "with the user's own permissions; if it is not allowed, find another "
            "way.\n"
            "- Keep the command to the step that needs it. Do not combine it "
            "with additional_write_folders."
        ),
    ),
    *WRITE_FOLDER_REQUEST_FIELD_PRESENTATION,
)

# Where a command connected through Pantaray's proxy.
_NETWORK_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer"},
            "connections": {"type": "integer"},
            "failed": {"type": "integer"},
            "bytes_sent": {"type": "integer"},
            "bytes_received": {"type": "integer"},
        },
    },
}

# Shared with run_python, which returns the same command result.
COMMAND_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "exit_code": {"type": "integer"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "network": _NETWORK_OUTPUT_SCHEMA,
    },
    "required": ["status", "exit_code", "stdout", "stderr"],
    "additionalProperties": False,
}

# A process started with run_in_background, as bash, read_process_output and
# stop_process return it.
PROCESS_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "properties": {
        "process_id": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "running",
                "exited",
                "stopped",
                "limit_exceeded",
                "spawn_failed",
                "failed",
            ],
        },
        "exit_code": {"type": "integer"},
        "signal": {"type": "integer"},
        "error": {"type": "string"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "stdout_dropped_bytes": {"type": "integer"},
        "stderr_dropped_bytes": {"type": "integer"},
        "network": _NETWORK_OUTPUT_SCHEMA,
        "llm_feedback": {"type": "string"},
    },
    "required": [
        "process_id",
        "status",
        "stdout",
        "stderr",
        "stdout_dropped_bytes",
        "stderr_dropped_bytes",
        "network",
    ],
    "additionalProperties": False,
}

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
                "use_login_environment is set or the command runs outside the "
                "sandbox, are temporary and removed when the command ends. Commands "
                "and descendants follow the user's command network setting and "
                "workspace file permissions. With network on, connections go "
                "through Pantaray's proxy, which HTTP(S) clients and git (also "
                "over ssh) pick up from the environment; clients that connect "
                "directly (ssh to a host, database clients, raw sockets, UDP) are "
                "blocked, so set run_outside_sandbox when one is needed. "
                "Commands cannot read or write files "
                "that hold secrets, such as .env files and credential stores "
                "(~/.ssh keys, ~/.aws/credentials); with use_login_environment "
                "the credential stores become readable, .env files never. "
                "Without run_in_background, the call waits for the command to end, "
                "with a finite execution timeout, and anything it leaves running "
                "is killed then; start dev servers and watchers with "
                "run_in_background instead. Do not use interactive prompts or "
                "sudo. Shell startup files are not loaded."
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
        output_schema={"oneOf": [COMMAND_OUTPUT_SCHEMA, PROCESS_OUTPUT_SCHEMA]},
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
