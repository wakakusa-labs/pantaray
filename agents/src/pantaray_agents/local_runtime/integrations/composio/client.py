"""Composio's REST API, called directly with the user's project API key.

This is the only holder of the key. Composio's own error text never leaves this
package: it is not instructions for the model, and an invalid-key reply quotes
part of the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import httpx2

from pantaray_agents.schema.agent.base import JSONValue

BASE_URL: Final = "https://backend.composio.dev/api/v3.1"
# Every connection of this desktop lives under one Composio user: the project
# is the user's own, and Composio forbids 'default' in production. Not Final:
# the live check points it at the owner's dev user.
USER_ID = "pantaray-desktop"
# Listing calls sit on every Action turn's 5 s budget and on the settings poll.
METADATA_TIMEOUT: Final = httpx2.Timeout(10.0, connect=5.0)
# A tool call runs a provider API behind Composio; a large read can take a while.
EXECUTE_TIMEOUT: Final = httpx2.Timeout(60.0, connect=5.0)
_PAGE_LIMIT: Final = 1000

type Method = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class ComposioResponse:
    status: int
    # The decoded JSON body; None when the body is not JSON.
    body: JSONValue
    retry_after_seconds: int | None

    @property
    def code(self) -> int | None:
        """Composio's own error code, such as 4302 or 812."""
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(code, int | float):
                    return int(code)
        return None


class ComposioRequestError(Exception):
    """A listing call failed; carries the status and code, never Composio's text."""

    def __init__(self, response: ComposioResponse) -> None:
        super().__init__(f"composio status={response.status} code={response.code}")
        self.response = response


class ComposioClient:
    __slots__ = ("_headers", "_transport")

    def __init__(
        self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._headers = {"x-api-key": api_key}
        self._transport = transport

    def __repr__(self) -> str:
        return "ComposioClient()"

    async def send(
        self,
        method: Method,
        path: str,
        *,
        timeout: httpx2.Timeout,
        json: JSONValue = None,
        params: dict[str, str] | None = None,
    ) -> ComposioResponse:
        """Raises httpx2.TransportError when no response arrived."""

        # A client per call: callers run on several threads, each with its own
        # event loop, and a pooled connection is bound to the loop that opened it.
        # Design limit: one TLS handshake per call; share a client per loop when
        # a call's p95 exceeds 1 s.
        async with httpx2.AsyncClient(
            base_url=BASE_URL,
            headers=self._headers,
            timeout=timeout,
            transport=self._transport,
        ) as client:
            response = await client.request(method, path, json=json, params=params)
        try:
            body: JSONValue = response.json()
        except ValueError:
            body = None
        retry_after = response.headers.get("retry-after", "")
        return ComposioResponse(
            status=response.status_code,
            body=body,
            retry_after_seconds=int(retry_after) if retry_after.isdigit() else None,
        )

    async def list_all(self, path: str, params: dict[str, str]) -> list[JSONValue]:
        """Every item of a paged listing, following ``next_cursor``.

        Raises ComposioRequestError on a non-200 reply and httpx2.TransportError
        when no reply arrived.
        """

        items: list[JSONValue] = []
        cursor: str | None = None
        while True:
            page_params = {**params, "limit": str(_PAGE_LIMIT)}
            if cursor is not None:
                page_params["cursor"] = cursor
            response = await self.send(
                "GET", path, timeout=METADATA_TIMEOUT, params=page_params
            )
            body = response.body
            if response.status != 200 or not isinstance(body, dict):
                raise ComposioRequestError(response)
            page = body.get("items")
            if isinstance(page, list):
                items.extend(page)
            next_cursor = body.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return items
            cursor = next_cursor
