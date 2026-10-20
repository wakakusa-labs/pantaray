"""Action専用の非ストリーミング発話・ツール呼び出し契約。"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from pantaray_llm.contracts.conversation import LlmConversation
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolDefinition,
    validate_tool_names,
)

# Progress messages beside tool calls stay short; longer deliverables belong in
# the final answer. A turn without calls is that answer, so the limit skips it.
ACTION_COMMENTARY_MAX_CHARACTERS = 4_000


class LlmCommentary(BaseModel):
    """What the model said in an Action turn.

    The phase stays ``commentary`` whatever a provider labelled the text: the
    caller reads a turn without calls as the reply that ends it and a turn with
    calls as progress, so the provider's own label decides nothing here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["commentary"]
    source_message_id: str = Field(min_length=1)
    text: str = Field(min_length=1, pattern=r"\S")


class LlmActionTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["action_turn"]
    tools: list[LlmToolDefinition] = Field(min_length=1)
    max_parallel_tool_calls: int = Field(default=1, ge=1)
    # Absent: the adapter sends the request's own user message and nothing else.
    # Present: those items follow that message, in order.
    conversation: LlmConversation | None = None

    @model_validator(mode="after")
    def validate_declared_tools(self) -> LlmActionTurnRequest:
        validate_tool_names(self.tools)
        return self

    @model_serializer(mode="wrap")
    def serialize_without_absent_conversation(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        """Serialize a request that carries no conversation exactly as before.

        Pantaray Cloud validates this contract with ``extra="forbid"``, and a
        deployed Cloud that predates ``conversation`` rejects the key whatever
        it holds -- ``null`` included -- which would fail every cloud-routed
        Action turn. The key is dropped here rather than in the one caller that
        builds the cloud envelope so that every serializer of this contract,
        including the tests that pin that envelope, produces the one wire.
        """

        payload: dict[str, object] = handler(self)
        if self.conversation is None:
            payload.pop("conversation", None)
        return payload


class LlmActionTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["action_turn"]
    messages: list[LlmCommentary]
    calls: list[LlmToolCall]
    dropped_call_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_turn_contents(self) -> LlmActionTurnResponse:
        if not self.messages and not self.calls:
            raise ValueError("an Action turn must contain commentary or tool calls")
        if self.calls and any(
            len(message.text) > ACTION_COMMENTARY_MAX_CHARACTERS
            for message in self.messages
        ):
            raise ValueError(
                "commentary beside tool calls must stay within "
                f"{ACTION_COMMENTARY_MAX_CHARACTERS} characters"
            )
        return self
