"""The ``browser`` tool's arguments, output, failure codes and their hints.

Actions Electron runs as they are share their model with the wire operation. Page
text appears in the output's ``data`` and nowhere else.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BROWSER_MAX_CONTACTED_HOSTS,
    BrowserContactedHost,
    BrowserNote,
    BrowserPageData,
    BrowserRelayFailureCode,
    BrowserTruncation,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.user_browser import BrowserChoice, UserBrowserId
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


# The relay's codes, and those the broker raises before Electron is asked.
BrowserFailureCode = (
    BrowserRelayFailureCode
    | Literal[
        "BROWSER_NETWORK_OFF", "BROWSER_OUTSIDE_READ_SCOPE", "BROWSER_USER_NOT_OFFERED"
    ]
)


_SNAPSHOT_AGAIN: Final = "Take a new snapshot and use a ref from it."
_RETRY_ONCE: Final = (
    "Try once more; if it fails again, tell the user the browser could not be "
    "used. The desktop app must be open."
)
_NAVIGATE_AGAIN: Final = "The page was discarded. Navigate again to continue."
_USER_CAN_FIX: Final = (
    "Tell the user, in their language, what the message says, and wait for them "
    "before trying their browser again."
)
FIX_HINTS: Final[dict[BrowserFailureCode, str]] = {
    "BROWSER_NO_PAGE": "Open a page with navigate first.",
    "BROWSER_REF_UNKNOWN": _SNAPSHOT_AGAIN,
    "BROWSER_REF_STALE": f"The page changed since the snapshot. {_SNAPSHOT_AGAIN}",
    "BROWSER_NAVIGATION_FAILED": (
        "Check the URL or path. For a local dev server, check with "
        "read_process_output that it is running."
    ),
    "BROWSER_TIMED_OUT": _NAVIGATE_AGAIN,
    "BROWSER_RENDERER_CRASHED": _NAVIGATE_AGAIN,
    "BROWSER_SESSION_GONE": "The page was closed. Navigate again to continue.",
    "BROWSER_ACTION_FAILED": (
        "Take a snapshot to see the page as it is now, then try another way."
    ),
    "BROWSER_USER_NOT_RUNNING": _USER_CAN_FIX,
    "BROWSER_USER_DEBUGGING_OFF": _USER_CAN_FIX,
    "BROWSER_USER_FILE_BLOCKED": _USER_CAN_FIX,
    "BROWSER_USER_DENIED": (
        "The user declined. Use their browser again only if they ask you to."
    ),
    "BROWSER_USER_UNAVAILABLE": "Tell the user their browser could not be used.",
    "BROWSER_NETWORK_OFF": (
        "The command network setting is off, which blocks every URL, localhost "
        "included. Open a local file instead, or ask the user to turn it on."
    ),
    "BROWSER_OUTSIDE_READ_SCOPE": (
        "Open a file read may open (see Workspace Path Rules), or an http(s) URL."
    ),
    "BROWSER_USER_NOT_OFFERED": (
        "Use the app's page (leave browser out) for local files, localhost and "
        "print; open only http(s) sites in the user's browser."
    ),
    "BROWSER_RELAY_TIMED_OUT": _RETRY_ONCE,
    "BROWSER_INTEGRITY_FAILED": _RETRY_ONCE,
}


class BrowserNavigateArgs(_StrictModel):
    action: Literal["navigate"]
    # An http(s) URL, or a local path resolved as ``read`` resolves it.
    url: str = Field(min_length=1, pattern=r"\S")
    # The approval question for a site the user's browser has not been allowed
    # to open in this run.
    justification: str | None = Field(default=None, min_length=1, pattern=r"\S")


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
    # Lowercase, as Electron compares hosts, so one host is approved once.
    host: Annotated[BrowserHost, AfterValidator(str.lower)]
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


class BrowserToolCall(_StrictModel):
    """A ``browser`` call: the page it drives, and what to do there.

    The model sends one flat object; ``browser`` is taken out before the rest
    parses as the action, so it never reaches an operation sent to Electron.
    """

    browser: BrowserChoice
    call: BrowserToolArgs

    @model_validator(mode="before")
    @classmethod
    def _split_browser(cls, data: Any) -> Any:
        # Any: the raw tool arguments, before they are parsed.
        if not isinstance(data, dict):
            return data
        call = dict(data)
        browser = call.pop("browser", None)
        return {"browser": "pantaray" if browser is None else browser, "call": call}


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
    attachments: list[BrowserImageAttachment]
    # Printed pages only: the page count of the print layout, and what was
    # drawn with each image's ref.
    page_count: int | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _has_images(self) -> Self:
        # Printed pages draw none when every page asked for is past the end.
        if not self.attachments and self.page_count is None:
            raise ValueError("a screenshot attaches at least one image")
        return self


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
    browser: BrowserChoice
    # The user's browser the "user" page runs in, chosen as the call was
    # validated; None on the app's page and for close.
    user_browser: UserBrowserId | None
    args: BrowserToolArgs

    @model_validator(mode="after")
    def _approval_gated_actions(self) -> Self:
        # print writes a file, allow_writes lets the page write and a navigate
        # in the user's browser opens a site there, so each is accepted only
        # with its approval.
        if isinstance(self.args.root, (BrowserPrintArgs, BrowserAllowWritesArgs)) or (
            self.browser == "user" and isinstance(self.args.root, BrowserNavigateArgs)
        ):
            raise ValueError(
                f"{self.args.root.action} is accepted as an approval-gated request"
            )
        return self


class _GatedBrowserRequest(_StrictModel):
    """A ``browser`` call the broker accepted with the approval it holds (none
    when no approval is needed), and the summary that approval names."""

    tool_invocation_id: str
    manifest_id: str
    execution_session_id: str
    action_id: str
    approval_session_id: str | None = None
    approval_source: ApprovalSource | None = None
    command_summary_json: dict[str, JSONValue]
    tool_request_id: str
    requested_at: str
    preflight_only: bool


class ValidatedBrowserPrintRequest(_GatedBrowserRequest):
    """``print``: the destination must still match the summary when it is saved."""

    args: BrowserPrintArgs


class ValidatedBrowserWriteRequest(_GatedBrowserRequest):
    """``allow_writes``: no approval once the page may write to the host."""

    browser: BrowserChoice
    args: BrowserAllowWritesArgs


class ValidatedUserBrowserReadRequest(_GatedBrowserRequest):
    """``navigate`` in the user's browser: no approval once the run's tab there
    may open the host."""

    user_browser: UserBrowserId
    # Lowercase, as the user's browser compares hosts.
    host: BrowserHost
    args: BrowserNavigateArgs
