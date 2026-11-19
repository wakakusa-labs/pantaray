"""The ``browser`` tool's arguments, output and failure codes.

Actions Electron runs as they are share their model with the wire operation. Page
text appears in the output's ``data`` and nowhere else.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BROWSER_MAX_CONTACTED_HOSTS,
    BrowserContactedHost,
    BrowserEngineFailureCode,
    BrowserNote,
    BrowserPageData,
    BrowserTruncation,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.websocket.server_messages import (
    BrowserBackOperation,
    BrowserClickOperation,
    BrowserConsoleOperation,
    BrowserHost,
    BrowserHoverOperation,
    BrowserPressOperation,
    BrowserRef,
    BrowserScrollOperation,
    BrowserSelectOperation,
    BrowserSnapshotOperation,
    BrowserTypeOperation,
    BrowserWaitOperation,
)
from pantaray_agents.tools.files.render_pages import PageNumbers

from ..models import ApprovalSource


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Electron's codes, and those only the runtime raises: refused before Electron is
# asked, or a missing or unverifiable answer.
BrowserFailureCode = (
    BrowserEngineFailureCode
    | Literal[
        "BROWSER_NETWORK_OFF",
        "BROWSER_OUTSIDE_READ_SCOPE",
        "BROWSER_RELAY_TIMED_OUT",
        "BROWSER_INTEGRITY_FAILED",
    ]
)


class BrowserNavigateArgs(_StrictModel):
    action: Literal["navigate"]
    # An http(s) URL, or a local path resolved as ``read`` resolves it.
    url: str = Field(min_length=1, pattern=r"\S")


class BrowserScreenshotArgs(_StrictModel):
    # The viewport, or exactly one of ``ref``, ``full_page`` and ``printed_pages``.
    action: Literal["screenshot"]
    ref: BrowserRef | None = None
    full_page: Literal[True] | None = None
    # 1-based pages of the print layout.
    printed_pages: PageNumbers | None = None

    @model_validator(mode="after")
    def _one_region(self) -> Self:
        chosen = (self.ref, self.full_page, self.printed_pages)
        if sum(region is not None for region in chosen) > 1:
            raise ValueError(
                "screenshot takes at most one of ref, full_page and printed_pages"
            )
        return self


class BrowserPrintArgs(_StrictModel):
    action: Literal["print"]
    pdf_path: str = Field(pattern=r"\S.*\.[Pp][Dd][Ff]$")


class BrowserAllowWritesArgs(_StrictModel):
    action: Literal["allow_writes"]
    host: BrowserHost
    justification: str = Field(min_length=1, pattern=r"\S")


class BrowserCloseArgs(_StrictModel):
    action: Literal["close"]


class BrowserToolArgs(
    RootModel[
        Annotated[
            BrowserNavigateArgs
            | BrowserBackOperation
            | BrowserSnapshotOperation
            | BrowserClickOperation
            | BrowserHoverOperation
            | BrowserTypeOperation
            | BrowserPressOperation
            | BrowserSelectOperation
            | BrowserScrollOperation
            | BrowserWaitOperation
            | BrowserScreenshotArgs
            | BrowserPrintArgs
            | BrowserConsoleOperation
            | BrowserAllowWritesArgs
            | BrowserCloseArgs,
            Field(discriminator="action"),
        ]
    ]
):
    model_config = ConfigDict(strict=True)


class BrowserImageAttachment(_StrictModel):
    # A stored screenshot or printed page, named by its user-scoped storage path.
    type: Literal["file"]
    source_kind: Literal["local_image_blob"]
    mime_type: str
    path: str
    storage_path: str
    ref: str
    byte_size: int
    # Printed pages only.
    page_number: int | None = None


class _BrowserPageOutput(_StrictModel):
    status: Literal["ok"]
    truncated: BrowserTruncation | None
    notes: list[BrowserNote]
    network: list[BrowserContactedHost] = Field(max_length=BROWSER_MAX_CONTACTED_HOSTS)
    untrusted: Literal[True]
    data: BrowserPageData


class BrowserPageOutput(_BrowserPageOutput):
    action: Literal[
        "navigate",
        "back",
        "snapshot",
        "click",
        "hover",
        "type",
        "press",
        "select",
        "scroll",
        "wait",
        "console",
    ]


class BrowserScreenshotOutput(_BrowserPageOutput):
    action: Literal["screenshot"]
    attachments: list[BrowserImageAttachment] = Field(min_length=1)


class BrowserPrintOutput(_BrowserPageOutput):
    action: Literal["print"]
    pdf_path: str
    replaced: bool


class BrowserControlOutput(_StrictModel):
    # ``allow_writes`` and ``close``: no page is read, so there is no data.
    status: Literal["ok"]
    action: Literal["allow_writes", "close"]


class BrowserToolOutput(
    RootModel[
        Annotated[
            BrowserPageOutput
            | BrowserScreenshotOutput
            | BrowserPrintOutput
            | BrowserControlOutput,
            Field(discriminator="action"),
        ]
    ]
):
    model_config = ConfigDict(strict=True)


class ValidatedBrowserRequest(_StrictModel):
    """A ``browser`` call the broker accepted, with the call's identity."""

    tool_invocation_id: str | None = None
    manifest_id: str
    execution_session_id: str
    action_id: str
    tool_request_id: str
    requested_at: str
    args: BrowserToolArgs

    @model_validator(mode="after")
    def _print_is_a_write(self) -> Self:
        # print writes a file, so it is accepted only with its approval.
        if isinstance(self.args.root, BrowserPrintArgs):
            raise ValueError("print is accepted as a ValidatedBrowserPrintRequest")
        return self


class ValidatedBrowserPrintRequest(_StrictModel):
    """A ``browser`` ``print`` the broker accepted: the approval it holds and the
    summary that approval names, which the destination must still match."""

    tool_invocation_id: str
    manifest_id: str
    execution_session_id: str
    action_id: str
    approval_session_id: str | None = None
    approval_source: ApprovalSource | None = None
    args: BrowserPrintArgs
    command_summary_json: dict[str, JSONValue]
    tool_request_id: str
    requested_at: str
    preflight_only: bool
