"""Ask Electron main to print one HTML file to PDF.

Electron answers with ``POST /local/action-html-render`` after writing the PDF to
``{artifact_root}/.tmp/html-renders/{render_request_id}.pdf``; both sides derive that
path from the id. The answer's ``byte_size`` and ``sha256`` are claims: the PDF is
re-read and verified before any byte is returned, and the file is removed on every
path, answered or not.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_regular_file_descriptor,
)
from pantaray_agents.schema.websocket.server_messages import (
    HtmlRenderRequestedMessage,
)

from .deliverable_sessions import deliver_desktop_request
from .one_shot_requests import OneShotRequests
from .runtime_env import read_local_runtime_artifact_root

HTML_RENDER_TEMP_DIRECTORY = ".tmp/html-renders"
# Electron gives itself 20 s per render; the rest covers queueing behind another
# render and the answer's round trip.
HTML_RENDER_TIMEOUT_SECONDS = 30.0

BlockedRequestReason = Literal["outside_read_scope", "network_off", "not_allowed"]

HtmlRendererFailureCode = Literal[
    "HTML_LOAD_FAILED",
    "HTML_RENDER_TIMED_OUT",
    "HTML_RENDERER_CRASHED",
    "HTML_PRINT_FAILED",
    "HTML_PDF_TOO_LARGE",
]

HtmlRenderFailureCode = (
    HtmlRendererFailureCode
    | Literal["HTML_RENDER_RELAY_TIMED_OUT", "HTML_RENDER_INTEGRITY_FAILED"]
)


@dataclass(frozen=True, slots=True)
class BlockedRequest:
    """A sub-resource the page asked for and Electron refused to load."""

    reason: BlockedRequestReason
    url: str


@dataclass(frozen=True, slots=True)
class HtmlRenderClaimed:
    """Electron's account of the PDF it wrote; every field is a claim."""

    byte_size: int
    sha256: str
    blocked_requests: tuple[BlockedRequest, ...]


@dataclass(frozen=True, slots=True)
class HtmlRenderFailed:
    code: HtmlRenderFailureCode
    message: str
    blocked_requests: tuple[BlockedRequest, ...]


@dataclass(frozen=True, slots=True)
class HtmlRendered:
    """A verified PDF. The bytes are returned because the file is already gone."""

    pdf: bytes
    blocked_requests: tuple[BlockedRequest, ...]


type HtmlRenderAnswer = HtmlRenderClaimed | HtmlRenderFailed
type HtmlRenderOutcome = HtmlRendered | HtmlRenderFailed

_BROKER = OneShotRequests[HtmlRenderAnswer]()


def html_render_broker() -> OneShotRequests[HtmlRenderAnswer]:
    """The process-wide broker shared by the render tools and the HTTP endpoint."""

    return _BROKER


async def request_html_render(
    *,
    user_id: str,
    action_id: str,
    process_id: str,
    tool_request_id: str,
    html_path: Path,
    file_roots: Sequence[Path],
    private_roots: Sequence[Path],
    private_root_exceptions: Sequence[Path],
    allow_network: bool,
) -> HtmlRenderOutcome:
    """Render ``html_path`` in Electron and return the verified PDF.

    The caller has already authorized ``html_path`` and decided what the page may
    load: ``file_roots`` and the private roots mirror the read policy, and
    ``allow_network`` is the user's command network setting.
    """

    artifact_root = read_local_runtime_artifact_root()
    broker = html_render_broker()
    render_request_id = broker.open(user_id=user_id)
    relative_pdf_path = f"{HTML_RENDER_TEMP_DIRECTORY}/{render_request_id}.pdf"
    try:
        await deliver_desktop_request(
            user_id,
            HtmlRenderRequestedMessage(
                kind="action",
                process_id=process_id,
                action_id=action_id,
                tool_request_id=tool_request_id,
                render_request_id=render_request_id,
                html_path=str(html_path),
                file_roots=[str(root) for root in file_roots],
                private_roots=[str(root) for root in private_roots],
                private_root_exceptions=[str(root) for root in private_root_exceptions],
                allow_network=allow_network,
            ),
        )
        answer = await broker.wait(
            request_id=render_request_id,
            timeout_seconds=HTML_RENDER_TIMEOUT_SECONDS,
        )
        if answer is None:
            return HtmlRenderFailed(
                code="HTML_RENDER_RELAY_TIMED_OUT",
                message=(
                    "The desktop app did not answer the render request in "
                    f"{int(HTML_RENDER_TIMEOUT_SECONDS)}s."
                ),
                blocked_requests=(),
            )
        if isinstance(answer, HtmlRenderFailed):
            return answer
        # Off the event loop: the PDF may run to 100 MB, and this loop also serves
        # the local HTTP API and the WebSocket.
        return await asyncio.to_thread(
            _read_claimed_pdf,
            artifact_root=artifact_root,
            relative_pdf_path=relative_pdf_path,
            claimed=answer,
        )
    finally:
        # Closing is idempotent; it matters when the delivery itself failed.
        broker.close(request_id=render_request_id)
        (artifact_root / relative_pdf_path).unlink(missing_ok=True)


def _read_claimed_pdf(
    *,
    artifact_root: Path,
    relative_pdf_path: str,
    claimed: HtmlRenderClaimed,
) -> HtmlRendered | HtmlRenderFailed:
    def integrity_failed(reason: str) -> HtmlRenderFailed:
        return HtmlRenderFailed(
            code="HTML_RENDER_INTEGRITY_FAILED",
            message=(
                f"The rendered PDF does not match the desktop app's answer: {reason}."
            ),
            blocked_requests=claimed.blocked_requests,
        )

    try:
        descriptor = open_regular_file_descriptor(
            root_path=artifact_root,
            relative_path=relative_pdf_path,
        )
    except DescriptorPathError:
        return integrity_failed("no regular file was written")
    try:
        # Checked before reading, so a wrong claim never pulls an unbounded file
        # into memory.
        if os.fstat(descriptor).st_size != claimed.byte_size:
            return integrity_failed("byte size differs")
        with open(descriptor, "rb", closefd=False) as handle:
            pdf = handle.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(pdf).hexdigest() != claimed.sha256:
        return integrity_failed("sha256 differs")
    return HtmlRendered(pdf=pdf, blocked_requests=claimed.blocked_requests)


__all__ = [
    "HTML_RENDER_TEMP_DIRECTORY",
    "HTML_RENDER_TIMEOUT_SECONDS",
    "BlockedRequest",
    "BlockedRequestReason",
    "HtmlRenderAnswer",
    "HtmlRenderClaimed",
    "HtmlRenderFailed",
    "HtmlRenderFailureCode",
    "HtmlRenderOutcome",
    "HtmlRendered",
    "HtmlRendererFailureCode",
    "html_render_broker",
    "request_html_render",
]
