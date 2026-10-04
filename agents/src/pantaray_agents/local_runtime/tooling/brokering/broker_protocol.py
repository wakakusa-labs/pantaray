from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from pantaray_agents.local_runtime.tooling.documents.page_render import (
    MAX_RENDERED_PAGES,
)
from pantaray_agents.schema.agent.base import JSONValue

from ..models import BrokerNetworkPolicy

BrokerExecutionPath = Literal[
    "broker_direct_read",
    "broker_direct_render_pdf",
    "broker_direct_list",
    "broker_direct_glob",
    "broker_direct_grep",
    "broker_direct_patch",
    "broker_sandbox_command",
    "broker_sandbox_python",
]
BrokerCapability = Literal["scoped_read", "scoped_write", "process_exec_local"]
BrokerApprovalPolicy = Literal["none", "prompt_each_time", "durable_grant"]
BrokerPathAccessKind = Literal["none", "read", "write", "exec"]
BrokerResourceKind = Literal["none", "lock", "process_group", "temp_dir"]
BrokerExecutionKind = Literal["agent_generated", "workspace_command"]
BrokerExecutableSourceKind = Literal[
    "app_runtime_python",
    "trusted_system_executable",
]
DiscoveryTruncationReason = Literal[
    "limit",
    "timeout",
    "output_bytes",
    "line_length",
]
DISCOVERY_RESULT_LIMIT_MAX = 500
LIST_MAX_DEPTH = 6


class ReadToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    offset: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1)
    start_unit: int | None = Field(default=None, ge=1)


class RenderPdfPageToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    pages: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, max_length=MAX_RENDERED_PAGES
    )

    @field_validator("pages")
    @classmethod
    def _reject_repeated_pages(cls, pages: list[int]) -> list[int]:
        # A repeat would spend one of the few page slots on an image the
        # caller already has, so it is a mistake to report rather than honour.
        if len(set(pages)) != len(pages):
            raise ValueError("pages must not name the same page twice")
        return pages


class ListToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    max_depth: int = Field(default=2, ge=1, le=LIST_MAX_DEPTH)
    limit: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


class GlobToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_path: str = Field(min_length=1, pattern=r"\S")
    pattern: str = Field(min_length=1, pattern=r"\S")
    limit: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


class GrepToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_path: str = Field(min_length=1, pattern=r"\S")
    pattern: str = Field(min_length=1, pattern=r"\S")
    include_glob: str | None = Field(default=None, min_length=1, pattern=r"\S")
    max_matches: int = Field(default=100, ge=1, le=DISCOVERY_RESULT_LIMIT_MAX)


class ApplyPatchEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    before_lines: list[str] = Field(
        default_factory=list,
        description="Existing nearby lines before old_lines, used only to locate the edit.",
    )
    old_lines: list[str] = Field(
        min_length=1,
        description="Existing contiguous lines to replace or delete.",
    )
    new_lines: list[str] = Field(description="Replacement lines.")
    after_lines: list[str] = Field(
        default_factory=list,
        description="Existing nearby lines after old_lines, used only to locate the edit.",
    )


class ApplyPatchAddChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["add"]
    path: str = Field(min_length=1)
    new_lines: list[str]
    trailing_newline: bool


class ApplyPatchUpdateChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["update"]
    path: str = Field(min_length=1)
    edits: list[ApplyPatchEdit] = Field(min_length=1, max_length=1)


class ApplyPatchDeleteChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["delete"]
    path: str = Field(min_length=1)


ApplyPatchChange = Annotated[
    ApplyPatchAddChange | ApplyPatchUpdateChange | ApplyPatchDeleteChange,
    Field(discriminator="op"),
]


class ApplyPatchToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changes: list[ApplyPatchChange] = Field(min_length=1, max_length=1)


WriteFolder = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _JustifiedCommandArgs(BaseModel):
    """Command access beyond the workspace defaults, and the reason for it.

    The justification is shown to the user as the approval question, so it is
    required exactly when the call asks for such access.
    """

    additional_write_folders: list[WriteFolder] = Field(default_factory=list)
    justification: str | None = Field(default=None, min_length=1, pattern=r"\S")

    def _asks_for_access(self) -> bool:
        return bool(self.additional_write_folders)

    @model_validator(mode="after")
    def _justified_exactly_when_asking(self) -> _JustifiedCommandArgs:
        if self._asks_for_access() != (self.justification is not None):
            raise ValueError(
                "justification is required with additional_write_folders, "
                "use_login_environment or run_outside_sandbox, and only then"
            )
        return self


class SandboxedBashToolArgs(_JustifiedCommandArgs):
    """The bash input an Action subagent is offered: no run outside the sandbox."""

    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, pattern=r"\S")
    cwd: str | None = Field(default=None, min_length=1, pattern=r"\S")
    use_login_environment: bool = False


class BashToolArgs(SandboxedBashToolArgs):
    run_outside_sandbox: bool = False

    def _asks_for_access(self) -> bool:
        return (
            self.use_login_environment
            or self.run_outside_sandbox
            or super()._asks_for_access()
        )

    @model_validator(mode="after")
    def _no_write_folders_outside_sandbox(self) -> BashToolArgs:
        # Outside the sandbox every folder is writable; asking for one would show
        # the user a folder approval that limits nothing.
        if self.run_outside_sandbox and self.additional_write_folders:
            raise ValueError(
                "additional_write_folders cannot be combined with run_outside_sandbox"
            )
        return self


class RunPythonToolArgs(_JustifiedCommandArgs):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, pattern=r"\S")
    args: list[str] = Field(default_factory=list)
    cwd: str | None = Field(default=None, min_length=1, pattern=r"\S")


class RenderedPdfPageAttachment(BaseModel):
    """One drawn page, carried as image bytes because no text stands for it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["file"]
    source_kind: Literal["local_image_blob"]
    mime_type: str
    page_number: int
    path: str
    storage_path: str
    ref: str
    byte_size: int


class RenderedPdfPagesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["pdf_pages"]
    path: str
    message: str
    page_count: int
    attachments: list[RenderedPdfPageAttachment]


class RendererPreparingOutput(BaseModel):
    """No pages yet: the Office renderer is still being installed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["renderer_preparing"]
    path: str
    message: str


class RenderPdfPageOutput(
    RootModel[
        Annotated[
            RenderedPdfPagesOutput | RendererPreparingOutput,
            Field(discriminator="kind"),
        ]
    ]
):
    model_config = ConfigDict(strict=True)


class DiscoveryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    kind: Literal["file", "directory"]
    name: str


class ListToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    entries: list[DiscoveryEntry]
    truncated: bool
    truncation_reason: DiscoveryTruncationReason | None = None
    retry_hint: str | None = None
    warning: str | None = None


class GlobToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    matches: list[DiscoveryEntry]
    truncated: bool
    truncation_reason: DiscoveryTruncationReason | None = None
    retry_hint: str | None = None
    warning: str | None = None
    skipped_files: int = Field(ge=0)


class GrepMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    line_number: int
    line: str


class GrepToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    matches: list[GrepMatch]
    truncated: bool
    truncation_reason: DiscoveryTruncationReason | None = None
    retry_hint: str | None = None
    warning: str | None = None
    skipped_files: int = Field(ge=0)


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: str
    message: str
    code: str | None = None
    llm_feedback: str | None = None
    exit_code: int | None = None


class ApplyPatchReadWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    start_line: int
    end_line: int
    match_reason: str
    text: str


class ApplyPatchToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    applied_paths: list[str]
    diff: str | None = None
    error: ToolError | None = None
    path: str | None = None
    patch_applied: bool | None = None
    read_scope: Literal["full_file", "target_windows"] | None = None
    file_truncated: bool | None = None
    text: str | None = None
    windows: list[ApplyPatchReadWindow] | None = None
    file_sha256: str | None = None
    llm_feedback: str | None = None


def apply_patch_tool_output_json_schema() -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], ApplyPatchToolOutput.model_json_schema())


class BashToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    exit_code: int
    stdout: str
    stderr: str
    signal: int | None = None
    error: ToolError | None = None


class RunPythonToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: str
    exit_code: int
    stdout: str
    stderr: str
    signal: int | None = None
    error: ToolError | None = None


class BrokerToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_id: str
    user_id: str
    manifest_id: str
    execution_session_id: str
    args: (
        ReadToolArgs
        | RenderPdfPageToolArgs
        | ListToolArgs
        | GlobToolArgs
        | GrepToolArgs
        | ApplyPatchToolArgs
        | BashToolArgs
        | RunPythonToolArgs
    )
    invocation_id: str | None = None
    tool_request_id: str | None = None
    requested_at: str | None = None
    preflight_only: bool = False


class ValidatedReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    path: str
    offset: int | None = None
    column: int | None = None
    limit: int | None = None
    start_unit: int | None = None


class ValidatedRenderPdfPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    path: str
    pages: list[int]


class ValidatedListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    path: str
    max_depth: int
    limit: int


class ValidatedGlobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    base_path: str
    pattern: str
    limit: int


class ValidatedGrepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    base_path: str
    pattern: str
    include_glob: str | None = None
    max_matches: int


class ValidatedPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str
    manifest_id: str
    execution_session_id: str
    action_id: str
    approval_session_id: str | None = None
    approval_source: Literal["settings", "prompt"] | None = None
    changes: list[ApplyPatchChange]
    patch_paths: list[str]
    command_summary_json: dict[str, JSONValue]
    tool_request_id: str
    requested_at: str
    preflight_only: bool


class ValidatedCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    approval_session_id: str | None = None
    approval_source: Literal["settings", "prompt"] | None = None
    action_plan_path: str
    private_storage_roots: list[str]
    action_workspace_root: str
    published_results_root: str
    app_runtime_python: str
    cwd: str
    command_summary_json: dict[str, JSONValue]
    argv: list[str]
    resolved_executable_path: str
    execution_kind: BrokerExecutionKind
    executable_source_kind: BrokerExecutableSourceKind
    env: dict[str, str]
    timeout_ms: int
    stdout_max_bytes: int
    stderr_max_bytes: int
    temp_storage_limit_bytes: int
    child_count_limit: int
    open_file_lease_limit: int
    network_policy: BrokerNetworkPolicy
    use_login_environment: bool
    # Only an approved Action bash call sets it; see build_validated_command_request.
    run_outside_sandbox: bool
    generated_python_code: str | None = None
    real_read_roots: list[str]
    real_write_roots: list[str]
    tool_request_id: str
    requested_at: str
    preflight_only: bool


class BrokerManagedToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_id: str
    execution_path: BrokerExecutionPath
    path_access_kind: BrokerPathAccessKind
    input_model_ref: str
    output_model_ref: str
    error_model_ref: str
    required_capabilities: list[BrokerCapability]
    approval_policy: BrokerApprovalPolicy
    resource_kinds: list[BrokerResourceKind]


class BrokerToolRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    definitions: dict[str, BrokerManagedToolDefinition]
