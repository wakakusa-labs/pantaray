"""Outbound websocket event helpers for suggestion processing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    CompletionChunkMessage,
    ProcessCompletedMessage,
    ProcessStartedMessage,
    SuggestionChunkMessage,
)


class SuggestionStreamEventsMixin:
    """Reusable outbound event emitters for suggestion workflows."""

    async def _emit_process_started(
        self,
        process_id: str,
        suggestion_id: str | None,
        *,
        kind: str,
    ) -> bool:
        return await self._send(
            OutboundEvent.PROCESS_STARTED.value,
            ProcessStartedMessage(
                kind=kind,
                process_id=process_id,
                suggestion_id=suggestion_id,
            ),
            process_id=process_id,
            meta={
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "kind": kind,
            },
        )

    async def _emit_completion_chunk(
        self,
        process_id: str,
        suggestion_id: str | None,
        content: str,
        *,
        kind: str,
    ) -> None:
        event_name = (
            OutboundEvent.SUGGESTION_CHUNK.value
            if kind == "suggestion"
            else OutboundEvent.COMPLETION_CHUNK.value
        )
        message = (
            SuggestionChunkMessage(content=content)
            if kind == "suggestion"
            else CompletionChunkMessage(content=content)
        )
        await self._send(
            event_name,
            message,
            process_id=process_id,
            meta={
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "kind": kind,
            },
        )

    async def _emit_process_completed(
        self,
        process_id: str,
        suggestion_id: str | None,
        *,
        status: str,
        has_suggestion: bool | None,
        kind: Literal["suggestion"],
        interaction_contract: str | None = None,
        artifacts: Sequence[str] = (),
    ) -> bool:
        sent = await self._send(
            OutboundEvent.PROCESS_COMPLETED.value,
            ProcessCompletedMessage(
                kind=kind,
                process_id=process_id,
                has_suggestion=has_suggestion,
                suggestion_id=suggestion_id,
                interaction_contract=interaction_contract,
                artifacts=list(artifacts),
                status=status,
            ),
            process_id=process_id,
            meta={
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "kind": kind,
            },
        )
        if sent:
            self.session_store.mark_process_completion_emitted(
                self.session_id,
                process_id,
                status=status,
            )
        return sent
