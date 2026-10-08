"""A tool's images ride its answer to the model, and the request as files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pantaray_agents.agents.core.llm_file_inputs import (
    build_blob_file_block,
    tool_image_file_input,
)
from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.budget import ContextBudget
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationEntry,
    ConversationRequest,
    ConversationRun,
    Finish,
    IdleTurn,
    run_conversation,
)
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.conversation.window import WindowState
from pantaray_agents.local_runtime.llm_proxy.request_builder import build_llm_request
from pantaray_agents.tools.files.read_only_tools import build_read_only_file_tools
from pantaray_agents.utils.llm_types import types
from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
)
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputImageBlock, LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition

from ..local_runtime.test_read_document_broker import PIXEL_PNG

_FINISH = LlmToolDefinition(
    name="finish", description="End.", parameters={"type": "object"}
)


@dataclass(frozen=True, slots=True)
class _Reply:
    response: LlmActionTurnResponse
    provider_turn: LlmProviderTurn | None = None


@dataclass(frozen=True, slots=True)
class _Totals:
    prompt_tokens: int = 0
    fields: dict[str, int] | None = None


def _reply(call: LlmToolCall) -> _Reply:
    return _Reply(LlmActionTurnResponse(mode="action_turn", messages=[], calls=[call]))


async def _run(tmp_path: Path, *, omit_before: int) -> list[ConversationRequest]:
    folder = (tmp_path / "home").resolve()
    folder.mkdir()
    (folder / "chart.png").write_bytes(PIXEL_PNG)
    tools = build_read_only_file_tools(
        db_path=tmp_path / "runtime.sqlite3",
        folders=(folder,),
        read_access_scope="workspace",
        app_storage_roots=(),
        spill_root=tmp_path / "spill",
    )
    read = LlmToolCall(
        call_id="c1", name="read", arguments={"path": str(folder / "chart.png")}
    )
    script = [
        _reply(read),
        _reply(LlmToolCall(call_id="c2", name="finish", arguments={})),
    ]
    sent: list[ConversationRequest] = []

    async def send(request: ConversationRequest) -> _Reply:
        sent.append(request)
        return script[len(sent) - 1]

    async def keep(_item: object) -> None:
        return None

    async def answer(call: LlmToolCall, result: object) -> LlmTurnToolResultItem:
        return LlmTurnToolResultItem(
            type="tool_result", call_id=call.call_id, name=call.name, output="ok"
        )

    def decide(turn: IdleTurn) -> Finish[str] | Continue:
        return Finish("done")

    async def nothing() -> list[LlmTurnUserItem]:
        return []

    await run_conversation(
        ConversationRun(
            prompt="head",
            system_instruction="system",
            tools=tools,
            ending_tools=(_FINISH,),
            history=[
                ConversationEntry(
                    LlmTurnUserItem(
                        type="user",
                        content=[LlmInputTextBlock(type="input_text", text="Look.")],
                    )
                )
            ],
            provider_turns=ProviderTurnStore(None),
            inference_profile="action.executing",
            max_turns=4,
            max_tool_calls=4,
            max_parallel_tool_calls=1,
            window=WindowState(
                budget=ContextBudget(
                    window_tokens=1_000_000, baseline=None, reset_pending=False
                ),
                omit_before=omit_before,
            ),
            usage=lambda: _Totals(fields={}),
            send=send,
            before_send=nothing,
            on_turn=keep,
            on_result=answer,
            on_notice=keep,
            decide=decide,
        )
    )
    return sent


@pytest.fixture(autouse=True)
def _cloud_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "cloud")


async def test_a_read_image_rides_its_answer_and_is_sent_as_its_file(
    tmp_path: Path,
) -> None:
    _first, second = await _run(tmp_path, omit_before=0)

    (image,) = second.images
    answer = second.conversation[-1]
    assert isinstance(answer, LlmTurnToolResultItem)
    (block,) = answer.content
    assert isinstance(block, LlmInputImageBlock)
    assert block.image.application_ref == image.ref == second.media_refs[0]
    # What a send hands the request builder: the item shows the image, and
    # the file under the same ref is uploaded with it, not inlined.
    built = build_llm_request(
        contents=[
            second.prompt,
            *(build_blob_file_block(tool_image_file_input(i)) for i in second.images),
        ],
        config=types.GenerateContentConfig(
            inference_profile="action.executing",
            system_instruction=second.system_instruction,
            tool_use=LlmActionTurnRequest(
                mode="action_turn",
                tools=list(second.tools),
                conversation=second.conversation,
            ),
        ),
        user_id="user-1",
        local_job_id="job-1",
    )
    ((blob_ref, (_name, payload, mime_type)),) = built.multipart_files
    assert (blob_ref, payload, mime_type) == (
        block.image.blob_ref,
        PIXEL_PNG,
        "image/png",
    )
    user_message = built.request.messages[-1]
    assert [b.type for b in user_message.content] == ["input_text"]


async def test_an_image_past_the_window_boundary_is_neither_shown_nor_sent(
    tmp_path: Path,
) -> None:
    # The read's answer (entry 2) lies before the boundary, as after a rebuild.
    _first, second = await _run(tmp_path, omit_before=3)

    answer = second.conversation[-1]
    assert isinstance(answer, LlmTurnToolResultItem) and answer.content == []
    assert (second.images, second.media_refs) == ((), ())
