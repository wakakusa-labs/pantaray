"""Electron's answer to one browser operation: the body of ``POST /local/action-browser``.

The PNG or PDF never crosses the endpoint: its ``byte_size`` and ``sha256`` are
claims the runtime verifies against the file. Everything in ``page_data`` was taken
from the page and is untrusted; no other field carries page text.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.schema.websocket.server_messages import BrowserHost

# Electron refuses a new host past this many per session, so no answer reports more.
BROWSER_MAX_CONTACTED_HOSTS: Final = 50
BROWSER_MAX_BLOCKED_REQUESTS: Final = 10


class _AnswerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


BrowserEngineFailureCode = Literal[
    "BROWSER_NO_PAGE",
    "BROWSER_REF_UNKNOWN",
    "BROWSER_REF_STALE",
    "BROWSER_NAVIGATION_FAILED",
    "BROWSER_TIMED_OUT",
    "BROWSER_RENDERER_CRASHED",
    "BROWSER_SESSION_GONE",
    # A valid page or ref, but Chromium could not carry the operation out.
    "BROWSER_ACTION_FAILED",
    # The user's browser is not running; the message names it.
    "BROWSER_USER_NOT_RUNNING",
    # macOS kept Pantaray from reading the browser's files.
    "BROWSER_USER_FILE_BLOCKED",
    # The user did not allow Pantaray to use the browser.
    "BROWSER_USER_DENIED",
    # Any other reason, which is not reported.
    "BROWSER_USER_UNAVAILABLE",
]

# What a relayed operation fails with: Electron's codes, or no answer in time, or
# an answer whose PNG or PDF does not match its claim.
BrowserRelayFailureCode = (
    BrowserEngineFailureCode
    | Literal["BROWSER_RELAY_TIMED_OUT", "BROWSER_INTEGRITY_FAILED"]
)

BrowserBlockedReason = Literal[
    "outside_read_scope",
    "network_off",
    "write_not_approved",
    "not_allowed",
    "too_many_hosts",
    # A page in the user's browser on a host this run was not approved to open.
    "read_not_approved",
]

# What Electron refused or dismissed on the page's behalf. A refused popup's URL is
# page data, so it goes in ``page_data.popups``.
BrowserNote = Literal["dialog_dismissed", "popup_refused", "download_refused"]

BrowserTruncation = Literal["snapshot_too_long", "page_too_tall", "console_too_long"]


class BrowserContactedHost(_AnswerModel):
    host: BrowserHost  # Never the URL: its path and query can carry tokens.
    port: int = Field(ge=1, le=65_535)


class BrowserBlockedRequest(_AnswerModel):
    reason: BrowserBlockedReason
    url: str


class BrowserConsoleMessage(_AnswerModel):
    level: Literal["debug", "log", "info", "warning", "error"]
    text: str


class BrowserPageData(_AnswerModel):
    """What the page showed after the operation. Untrusted, every field."""

    url: str
    title: str
    # The accessibility snapshot, for ``snapshot`` only.
    snapshot: str | None
    # The console lines, for ``console`` only.
    console: list[BrowserConsoleMessage] | None
    popups: list[str]
    blocked: list[BrowserBlockedRequest] = Field(
        max_length=BROWSER_MAX_BLOCKED_REQUESTS
    )


class BrowserPngClaim(_AnswerModel):
    kind: Literal["png"]
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)


class BrowserPdfClaim(_AnswerModel):
    kind: Literal["pdf"]
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


BrowserArtifactClaim = Annotated[
    BrowserPngClaim | BrowserPdfClaim, Field(discriminator="kind")
]


class BrowserOperationSucceeded(_AnswerModel):
    status: Literal["ok"]
    page_data: BrowserPageData
    notes: list[BrowserNote]
    truncated: BrowserTruncation | None
    # Hosts reached since the session's previous answer.
    network: list[BrowserContactedHost] = Field(max_length=BROWSER_MAX_CONTACTED_HOSTS)
    # A PNG for ``screenshot``, a PDF for ``print``; null for every other operation.
    artifact: BrowserArtifactClaim | None
    # The host a web navigate opened, as Chromium parsed its URL, so the runtime
    # never parses one itself; null for a file and every other operation.
    opened_host: BrowserHost | None


class BrowserOperationFailed(_AnswerModel):
    status: Literal["failed"]
    code: BrowserEngineFailureCode
    message: str  # Electron's own words, never page text.
    network: list[BrowserContactedHost] = Field(max_length=BROWSER_MAX_CONTACTED_HOSTS)


class BrowserOperationAnswer(_AnswerModel):
    operation_request_id: str = Field(min_length=1, pattern=r"\S")
    result: Annotated[
        BrowserOperationSucceeded | BrowserOperationFailed,
        Field(discriminator="status"),
    ]
