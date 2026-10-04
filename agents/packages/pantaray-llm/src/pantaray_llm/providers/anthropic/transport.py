"""Anthropic Messages API の固定エンドポイントへ非ストリーミング要求を送る。"""

from __future__ import annotations

from contextlib import nullcontext

import httpx

from pantaray_llm.contracts.json_value import JSONValue

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION_HEADER = "2023-06-01"
ANTHROPIC_MAX_REQUEST_BYTES = 32 * 1024 * 1024
# Applies only when no client is lent, which the desktop never does. It stays
# below the desktop's LLM read timeout so a caller waiting on this response gets
# an error back instead of giving up first.
ANTHROPIC_REQUEST_TIMEOUT_SECONDS = 180.0


async def send_anthropic_message(
    *,
    api_key: str,
    body: dict[str, JSONValue],
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    session = (
        nullcontext(client)
        if client is not None
        else httpx.AsyncClient(timeout=ANTHROPIC_REQUEST_TIMEOUT_SECONDS)
    )
    async with session as active_client:
        return await active_client.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION_HEADER,
                "content-type": "application/json",
            },
            json=body,
        )
