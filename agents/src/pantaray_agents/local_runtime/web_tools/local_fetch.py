"""Read web_extract pages from this computer when no web provider is connected.

Pantaray Cloud and a stored Tavily key both have Tavily fetch the page. With
neither, each page is fetched and turned into text by its own short-lived
worker process (``page_worker.py``), and every outcome is per URL.

Everything a page controls -- redirects, headers, encoding layers, markup, a
PDF -- is handled in that worker, never in the helper, because the ways a page
can make fetching or parsing hang, expand or crash have no end. The helper
bounds each worker from outside instead: a wall-clock limit after which it is
killed, and a limit on the size of its reply. The worker reports the outcomes
it recognises; whatever else becomes of a worker fails only its own page, with
one message, decided in one place here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from pantaray_agents.local_runtime.tooling.documents.sandboxed_worker import (
    sandboxed_worker,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.errors import PROXY_INVALID_INPUT, ProxyProvider
from pantaray_llm.web_tools.schemas import WebExtractArgs

from .contract import (
    WebContentExecutionError,
    WebToolsWrapperContext,
    WebToolsWrapperResponse,
)
from .page_worker import SIBLING_MODULE_FILES

logger = logging.getLogger(__name__)

WORKER_SCRIPT: Final = Path(__file__).with_name("page_worker.py")
# One page from start to text: redirects, the body and its conversion. The tool
# allows the whole call 60 s and pages run in parallel, so this leaves room for
# the call to answer with each page's own outcome.
PAGE_WALL_CLOCK_SECONDS: Final = 30.0
# The worker's text is at most a few times its 5 MB input; past this its reply
# is not read further and the page is unreadable.
MAX_WORKER_REPLY_BYTES: Final = 32 * 1024 * 1024
# A failure reason comes from the process that held the page, so only this much
# of it is passed on.
_MAX_REASON_CHARS: Final = 300
_UNREADABLE: Final = "The page could not be read."
LOCAL_PROVIDER: ProxyProvider = "local"


class _PageFailure(Exception):
    """One page could not be read; the message is what the model is told."""


async def extract_pages_locally(
    *,
    args: dict[str, JSONValue],
    context: WebToolsWrapperContext,
    web_tool_profile: str,
) -> WebToolsWrapperResponse:
    """Fetch every URL from this computer, in the result shape Tavily's has.

    ``query`` is not applied: there is no ranking here, so each page is
    returned whole, which the tool description states for this route.
    """

    try:
        request = WebExtractArgs.model_validate(args)
    except ValidationError as exc:
        raise WebContentExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message="The web_extract request is invalid.",
            retryable=False,
            request_id=context["request_id"],
            profile_id=web_tool_profile,
            upstream_provider=LOCAL_PROVIDER,
        ) from exc
    started_at = time.perf_counter()
    outcomes = await asyncio.gather(*(_read_page(url) for url in request.urls))
    elapsed = time.perf_counter() - started_at
    results: list[JSONValue] = []
    failed_results: list[JSONValue] = []
    for url, outcome in zip(request.urls, outcomes, strict=True):
        if isinstance(outcome, _PageFailure):
            failed_results.append({"url": url, "error": str(outcome)})
        else:
            results.append({"url": url, "raw_content": outcome})
    return {
        "tool_id": "web_extract",
        "status": "success",
        "request_id": context["request_id"],
        "response_time_ms": int(elapsed * 1000),
        "result": {
            "results": results,
            "failed_results": failed_results,
            "response_time": elapsed,
            "meta": {
                "request_id": context["request_id"],
                "upstream_provider": LOCAL_PROVIDER,
                "profile_id": web_tool_profile,
            },
        },
    }


async def _read_page(url: str) -> str | _PageFailure:
    async with sandboxed_worker(
        script=WORKER_SCRIPT,
        read_files=SIBLING_MODULE_FILES,
        python_executable=Path(sys.executable),
        allow_network=True,
    ) as process:
        try:
            reply = await asyncio.wait_for(
                _exchange(process, url), PAGE_WALL_CLOCK_SECONDS
            )
        except TimeoutError:
            reply = None
    return _outcome(reply, returncode=process.returncode)


async def _exchange(process: asyncio.subprocess.Process, url: str) -> bytes | None:
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(json.dumps({"url": url}).encode("utf-8") + b"\n")
        await process.stdin.drain()
        process.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        # Gone before it was asked; its missing reply says the rest.
        return None
    reply = bytearray()
    while chunk := await process.stdout.read(64 * 1024):
        reply += chunk
        if len(reply) > MAX_WORKER_REPLY_BYTES:
            return None
    return bytes(reply)


def _outcome(reply: bytes | None, *, returncode: int | None) -> str | _PageFailure:
    """The page's text, the failure the worker named, or the one failure for the rest."""

    try:
        parsed = json.loads(reply) if reply else None
        if isinstance(parsed, dict) and parsed.get("status") == "read":
            text = parsed["text"]
            if isinstance(text, str) and text.strip():
                return text
        if isinstance(parsed, dict) and parsed.get("status") == "failed":
            reason = parsed["reason"]
            if isinstance(reason, str) and reason.strip():
                return _PageFailure(reason[:_MAX_REASON_CHARS])
    except ValueError:
        pass
    logger.info("web_extract page worker gave no reply (exit status %s)", returncode)
    return _PageFailure(_UNREADABLE)


__all__ = ["LOCAL_PROVIDER", "extract_pages_locally"]
