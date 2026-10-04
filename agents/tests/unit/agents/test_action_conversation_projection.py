"""Action のターンを「構造を保った項目の列」へ写す投影のテスト。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter

from pantaray_agents.agents.action_agent.runtime.agents_md import (
    PANTARAY_DEFAULT_AGENTS_MD,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm import turn_input
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.builders import (
    _build_llm_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.models.checkpoint import (
    HistoryEntryModel,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
    PromptRenderingDeps,
    PromptRenderingService,
)
from pantaray_agents.agents.action_agent.support.conversation_projection import (
    OMITTED_OUTPUT_MARK,
    TURN_CONTEXT_HEADING,
    UNCHANGED_TURN_CONTEXT,
    ActionConversationProjection,
    project_action_conversation,
)
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.agents.action_agent.support.world_state import WorldState
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.tasks.internal_jobs import action_subagent as subagent_job
from pantaray_agents.utils.prompt_loader import PromptLoader
from pantaray_llm.contracts.conversation import (
    AnthropicProviderTurn,
    LlmConversation,
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.input_block import LlmInputImageBlock, LlmInputTextBlock

_NOW = "Current time: 2026-09-19T00:00:00Z"
_RENDERING = PromptRenderingService(
    PromptRenderingDeps(formatter=ActionAgentFormatter())
)
_IMAGE = b"screenshot-bytes"
_IMAGE_SHA256 = hashlib.sha256(_IMAGE).hexdigest()
_ATTACHMENT: dict[str, Any] = {
    "type": "file",
    "ref": "tool_attachment:shot",
    "blob_ref": "attachment_blob_shot",
    "display_path": "shot.png",
    "mime_type": "image/png",
    "byte_size": len(_IMAGE),
    "sha256": _IMAGE_SHA256,
    "source_kind": "workspace_file",
    "workspace_root_path": "/tmp",
    "workspace_relative_path": "shot.png",
}


# --- history fixtures ---------------------------------------------------------


def _entry(index: int, step_type: StepType, suffix: str, **overrides: Any) -> Any:
    return {
        "step_id": f"{suffix}-{index}",
        "step_number": index,
        "phase": "executing",
        "step_type": step_type,
        "summary": "",
        "tool_id": None,
        "started_at": "2026-09-19T00:00:00Z",
        "completed_at": "2026-09-19T00:00:01Z",
        "short_step_id": f"S-{index}-{suffix}",
    } | overrides


def _user(index: int, text: str = "調べてください", **overrides: Any) -> Any:
    return _entry(
        index, StepType.USER_REQUEST, "USER", user_request_text=text, **overrides
    )


def _commentary(index: int, text: str) -> Any:
    return _entry(
        index,
        StepType.ASSISTANT_MESSAGE,
        "MSG",
        assistant_message_text=text,
        assistant_phase="commentary",
    )


def _think(index: int, **overrides: Any) -> Any:
    return _entry(
        index,
        StepType.LLM_OUTPUT,
        "THINK",
        **{"turn_context": f"TC-{index}", **overrides},
    )


def _tool(index: int, *, think: int, call_id: str, **overrides: Any) -> Any:
    return _entry(
        index,
        StepType.TOOL_EXECUTION,
        "TOOL",
        **{
            "tool_id": "read",
            "summary": f"note {index}",
            "args": {"path": f"/tmp/{index}"},
            "output": {"kind": "file", "content": f"body {index}"},
            "result_line": f"read: ok, {index} chars",
            "call_id": call_id,
            "llm_step_id": f"THINK-{think}",
            **overrides,
        },
    )


def _project(
    entries: list[Any],
    *,
    omit: int = 0,
    turn_context: str | None = _NOW,
    repair: str = "",
    provider_turns: dict[str, Any] | None = None,
) -> ActionConversationProjection | None:
    return project_action_conversation(
        entries,
        omit_before_step_number=omit,
        turn_context=turn_context,
        repair_notice=repair,
        provider_turns=provider_turns or {},
    )


def _require(entries: list[Any], **kwargs: Any) -> list[Any]:
    projection = _project(entries, **kwargs)
    assert projection is not None
    # Anything the projection builds must also survive the shared contract,
    # whose validator is what rejects an unanswered or twice-answered call.
    TypeAdapter(LlmConversation).validate_python(projection.conversation)
    return list(projection.conversation)


def _text(item: Any) -> str:
    return cast(LlmInputTextBlock, item.content[0]).text


def _openai_turn(*calls: tuple[str, str], encrypted: str = "opaque") -> LlmProviderTurn:
    """What one Responses turn returns: its reasoning, then its calls."""

    return OpenAiProviderTurn(
        provider="openai",
        items=[
            {"type": "reasoning", "id": "rs_1", "encrypted_content": encrypted},
            *(
                {
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "{}",
                }
                for call_id, name in calls
            ),
        ],
    )


def _anthropic_turn(*calls: tuple[str, str]) -> LlmProviderTurn:
    return AnthropicProviderTurn(
        provider="anthropic",
        blocks=[
            {"type": "thinking", "thinking": "調べます。", "signature": "sig"},
            *(
                {"type": "tool_use", "id": call_id, "name": name, "input": {}}
                for call_id, name in calls
            ),
        ],
    )


# --- 履歴の行 → 項目 -----------------------------------------------------------


def test_the_first_turn_sends_the_request_and_this_turns_context() -> None:
    items = _require([_user(1)])

    assert [type(item) for item in items] == [LlmTurnUserItem, LlmTurnUserItem]
    assert _text(items[0]) == "調べてください"
    assert _text(items[-1]) == _NOW


def test_a_single_call_turn_replays_its_context_then_its_call_and_result() -> None:
    items = _require([_user(1), _think(2), _tool(2, think=2, call_id="call_a")])

    assert _text(items[1]) == "TC-2"
    assistant = cast(LlmTurnAssistantItem, items[2])
    assert assistant.text == []
    assert assistant.provider_turn is None
    assert [(call.call_id, call.name) for call in assistant.calls] == [
        ("call_a", "read")
    ]
    # Adoption split the note out of the arguments; a replayed call carries it.
    assert assistant.calls[0].arguments == {"path": "/tmp/2", "step_note": "note 2"}
    result = cast(LlmTurnToolResultItem, items[3])
    assert result.call_id == "call_a"
    assert result.output == {
        "result": "read: ok, 2 chars",
        "output": {"kind": "file", "content": "body 2"},
        "history_ref": "S-2-TOOL",
    }
    assert result.content == []


def test_a_think_recorded_before_the_context_field_replays_none() -> None:
    entry = _think(2)
    del entry["turn_context"]
    items = _require([_user(1), entry, _tool(2, think=2, call_id="call_a")])

    assert [type(item) for item in items] == [
        LlmTurnUserItem,
        LlmTurnAssistantItem,
        LlmTurnToolResultItem,
        LlmTurnUserItem,
    ]


def test_commentary_folds_into_the_assistant_item_of_the_think_it_came_with() -> None:
    items = _require(
        [
            _user(1),
            _commentary(2, "まず読みます"),
            _think(3),
            _tool(3, think=3, call_id="call_a"),
        ]
    )

    assistant = cast(LlmTurnAssistantItem, items[2])
    assert assistant.text == ["まず読みます"]
    assert len(assistant.calls) == 1


def test_a_commentary_only_turn_sends_text_without_calls() -> None:
    items = _require([_user(1), _commentary(2, "少し時間がかかります"), _think(3)])

    assistant = cast(LlmTurnAssistantItem, items[2])
    assert assistant.text == ["少し時間がかかります"]
    assert assistant.calls == []


def test_parallel_calls_share_one_assistant_item_in_declaration_order() -> None:
    items = _require(
        [
            _user(1),
            _think(2),
            # Appended out of order: completion order is not declaration order.
            _tool(4, think=2, call_id="call_c"),
            _tool(2, think=2, call_id="call_a"),
            _tool(3, think=2, call_id="call_b"),
        ]
    )

    assistant = cast(LlmTurnAssistantItem, items[2])
    assert [call.call_id for call in assistant.calls] == ["call_a", "call_b", "call_c"]
    assert [cast(LlmTurnToolResultItem, item).call_id for item in items[3:6]] == [
        "call_a",
        "call_b",
        "call_c",
    ]


def test_calls_the_batch_plan_dropped_reach_the_model_behind_their_results() -> None:
    notice = "Ran 1 of 2 requested tool calls. Not run this turn: bash (...)."
    items = _require(
        [
            _user(1),
            _think(2, result_line=notice),
            _tool(2, think=2, call_id="call_a"),
        ]
    )

    # The dropped call has no ``call_id`` to answer, so it cannot be a result.
    assert [type(item) for item in items] == [
        LlmTurnUserItem,
        LlmTurnUserItem,
        LlmTurnAssistantItem,
        LlmTurnToolResultItem,
        LlmTurnUserItem,
        LlmTurnUserItem,
    ]
    assert _text(items[4]).endswith(notice)


def test_a_think_that_ran_nothing_and_said_nothing_sends_only_its_notice() -> None:
    items = _require(
        [_user(1), _think(2, result_line="Execution THINK output invalid")]
    )

    assert [type(item) for item in items] == [LlmTurnUserItem] * 4
    assert "Execution THINK output invalid" in _text(items[2])


def test_a_call_stopped_before_it_ran_still_answers_its_call_id() -> None:
    items = _require(
        [
            _user(1),
            _think(2),
            _tool(
                2,
                think=2,
                call_id="call_a",
                result_line="read: not executed, stopped by user",
                output={"error": {"error_code": "ACTION_TOOL_NOT_EXECUTED"}},
            ),
        ]
    )

    result = cast(LlmTurnToolResultItem, items[3])
    assert result.call_id == "call_a"
    assert result.output["result"] == "read: not executed, stopped by user"  # type: ignore[index]


def test_a_user_message_arriving_mid_run_becomes_its_own_item() -> None:
    items = _require(
        [
            _user(1),
            _think(2),
            _tool(2, think=2, call_id="call_a"),
            _user(3, "やっぱり別の方を見てください"),
            _think(4),
            _tool(4, think=4, call_id="call_b"),
        ]
    )

    assert _text(items[4]) == "やっぱり別の方を見てください"
    assert _text(items[5]) == "TC-4"
    assert isinstance(items[6], LlmTurnAssistantItem)


# --- 省略と添付 ---------------------------------------------------------------


def test_an_omitted_turn_drops_its_context_body_and_media() -> None:
    entries = [
        _user(1),
        _think(2),
        _tool(2, think=2, call_id="call_a", attachments=[_ATTACHMENT]),
        _think(3),
        _tool(3, think=3, call_id="call_b"),
    ]
    projection = _project(entries, omit=3)
    assert projection is not None
    items = list(projection.conversation)

    # The rebuilt window keeps no stale context from before its boundary.
    assert [_text(item) for item in items if isinstance(item, LlmTurnUserItem)] == [
        "調べてください",
        "TC-3",
        _NOW,
    ]
    omitted = cast(LlmTurnToolResultItem, items[2])
    assert omitted.output == {
        "result": "read: ok, 2 chars",
        "output": "…",
        "history_ref": "S-2-TOOL",
    }
    assert omitted.content == []
    assert projection.file_inputs == ()
    kept = cast(LlmTurnToolResultItem, items[5])
    assert kept.output["output"] == {"kind": "file", "content": "body 3"}  # type: ignore[index]


def test_an_attached_image_rides_on_the_result_that_produced_it() -> None:
    projection = _project(
        [
            _user(1),
            _think(2),
            _tool(2, think=2, call_id="call_a", attachments=[_ATTACHMENT]),
        ]
    )
    assert projection is not None

    result = cast(LlmTurnToolResultItem, projection.conversation[3])
    assert result.output["attached_files"] == ["tool_attachment:shot"]  # type: ignore[index]
    block = cast(LlmInputImageBlock, result.content[0])
    assert block.image.blob_ref == "attachment_blob_shot"
    assert block.image.application_ref == "tool_attachment:shot"
    assert block.image.sha256 == _IMAGE_SHA256
    # The request has to upload exactly the media the items reference.
    assert [file_input["ref"] for file_input in projection.file_inputs] == [
        "tool_attachment:shot"
    ]


def test_a_non_image_attachment_is_refused_instead_of_sent_as_an_image() -> None:
    """A document reaches the model as the text `read` returns, never as bytes.

    History a pre-#1395 `read` wrote can still name a PDF attachment, and
    sending those bytes as an image would misdescribe them to the provider.
    """

    attachment = {**_ATTACHMENT, "mime_type": "application/pdf"}

    with pytest.raises(RuntimeError, match="not an image"):
        _project(
            [
                _user(1),
                _think(2),
                _tool(2, think=2, call_id="call_a", attachments=[attachment]),
            ]
        )


# --- 縮退 ---------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["call_id", "llm_step_id"])
def test_a_tool_row_without_its_call_identity_declines_the_whole_window(
    missing: str,
) -> None:
    row = _tool(2, think=2, call_id="call_a")
    del row[missing]

    assert _project([_user(1), _think(2), row]) is None


def test_a_call_id_a_later_turn_reuses_declines_the_whole_window() -> None:
    # Unpairable, and the contract would refuse it on every turn after this one.
    entries = [
        _user(1),
        _think(2),
        _tool(2, think=2, call_id="call_0"),
        _think(3),
        _tool(3, think=3, call_id="call_0"),
    ]

    assert _project(entries) is None


def test_an_answer_restored_before_a_follow_up_stays_ahead_of_it() -> None:
    # A restored run lists the last answer and then the follow-up, with no
    # THINK row between them to carry the answer.
    items = _require(
        [
            _user(1, "最初の依頼"),
            _think(2),
            _commentary(3, "前のターンの答えです"),
            _user(4, "追加の依頼"),
        ]
    )

    assert [item.type for item in items] == [
        "user",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert cast(LlmTurnAssistantItem, items[2]).text == ["前のターンの答えです"]
    assert _text(items[3]) == "追加の依頼"


# --- 前のターンの思考 ---------------------------------------------------------


@pytest.mark.parametrize(
    "turn", [_openai_turn(("call_a", "read")), _anthropic_turn(("call_a", "read"))]
)
def test_a_provider_turn_rides_on_the_think_that_produced_it(
    turn: LlmProviderTurn,
) -> None:
    items = _require(
        [_user(1), _think(2), _tool(2, think=2, call_id="call_a")],
        provider_turns={"THINK-2": turn},
    )

    assistant = cast(LlmTurnAssistantItem, items[2])
    assert assistant.provider_turn is turn
    # The structure the adapters check against stays alongside it, untouched.
    assert [call.call_id for call in assistant.calls] == ["call_a"]


@pytest.mark.parametrize(
    "turn",
    [
        # The batch plan deferred `call_b`, so nothing ever answers it.
        _openai_turn(("call_a", "read"), ("call_b", "read")),
        _anthropic_turn(("call_a", "read"), ("call_b", "read")),
    ],
)
def test_a_turn_that_does_not_describe_what_ran_is_left_behind(
    turn: LlmProviderTurn,
) -> None:
    items = _require(
        [_user(1), _think(2), _tool(2, think=2, call_id="call_a")],
        provider_turns={"THINK-2": turn},
    )

    assert cast(LlmTurnAssistantItem, items[2]).provider_turn is None


def test_an_omitted_turn_keeps_the_turn_its_item_already_sent() -> None:
    """境界が進んでも assistant 項目は書き換えない（省略するのは結果の本文）。"""

    turn = _openai_turn(("call_a", "read"))
    items = _require(
        [_user(1), _think(2), _tool(2, think=2, call_id="call_a"), _think(3)],
        omit=3,
        provider_turns={"THINK-2": turn},
    )

    assert cast(LlmTurnAssistantItem, items[1]).provider_turn is turn
    assert cast(LlmTurnToolResultItem, items[2]).output["output"] == OMITTED_OUTPUT_MARK


# --- 追記のみ -----------------------------------------------------------------


def test_a_later_turn_appends_to_everything_the_previous_turn_sent() -> None:
    """記録した turn context ごと、前のターンの会話が厳密な接頭辞になる。"""

    first_entries = [_user(1)]
    first = _require(first_entries, turn_context="TC-2")

    # The turn records the context it sent on the THINK row it then writes.
    second_entries = [
        *first_entries,
        _think(2),
        _tool(2, think=2, call_id="call_a"),
    ]
    second = _require(second_entries, turn_context="TC-3")
    assert second[: len(first)] == first

    third = _require(
        [*second_entries, _think(3), _tool(3, think=3, call_id="call_b")],
        turn_context="TC-4",
    )
    assert third[: len(second)] == second


def test_a_recorded_turn_keeps_each_turn_a_prefix_of_the_next() -> None:
    """応答の思考を記録しても、前のターンの会話は厳密な接頭辞のまま。"""

    first_entries = [_user(1)]
    first = _require(first_entries, turn_context="TC-2")

    # The run records turn 2's response before it projects turn 3, so the item
    # that carries the turn is written once and never revised.
    turns = {"THINK-2": _openai_turn(("call_a", "read"))}
    second_entries = [*first_entries, _think(2), _tool(2, think=2, call_id="call_a")]
    second = _require(second_entries, turn_context="TC-3", provider_turns=turns)
    assert second[: len(first)] == first
    assert cast(LlmTurnAssistantItem, second[2]).provider_turn is not None

    turns["THINK-3"] = _openai_turn(("call_b", "read"))
    third = _require(
        [*second_entries, _think(3), _tool(3, think=3, call_id="call_b")],
        turn_context="TC-4",
        provider_turns=turns,
    )
    assert third[: len(second)] == second
    assert cast(LlmTurnAssistantItem, third[5]).provider_turn is not None


def test_the_recorded_context_is_the_one_the_next_turn_replays() -> None:
    """1 ターン目を投影 → THINK を記録 → 2 ターン目を投影、で接頭辞になる。"""

    first_entries = [_user(1)]
    first = _prepare(first_entries, sends_conversation=True)
    assert first.conversation is not None

    recorded = _build_llm_history_entry(
        step_id="THINK-2",
        step_number=2,
        phase="executing",
        summary="",
        tool_id="read",
        started_at="2026-09-19T00:00:00Z",
        completed_at="2026-09-19T00:00:01Z",
        short_step_id="S-2-THINK",
        turn_context=first.turn_context,
        world_state=first.world_state,
    )
    # The same row survives a checkpoint, which is where a resumed run reads it.
    assert HistoryEntryModel.model_validate(recorded).turn_context == first.turn_context

    second = _require(
        [*first_entries, recorded, _tool(2, think=2, call_id="call_a")],
        turn_context="TC-next",
    )
    assert _text(second[1]) == first.turn_context
    assert second[: len(first.conversation)] == list(first.conversation)


# --- 新しい依頼をまたぐ追記のみ -------------------------------------------------

_RUN_1 = {
    "insight_data": "I-1",
    "agents_md_instructions": "# AGENTS.md instructions for ~/.pantaray\nA-1",
    "workspace_context_prompt": "W-1",
}


def _executing_agent() -> Any:
    """The production executing template, as the agent reads it."""

    config = PromptLoader().load_config("action/executing")
    return SimpleNamespace(
        executing_prompt=config.prompt,
        executing_system_instruction=config.system_instruction,
        DEFAULT_SYSTEM_INSTRUCTION="D",
        executing_role_rule=config.require_role_rule,
        executing_world_state_update=config.require_world_state_update,
    )


def _think_once(
    state: Any, *, think: int, call_id: str, omit: int = 0, now: str = "T0"
) -> tuple[str, Any]:
    """One THINK through the production template; its row is then recorded.

    The row goes through the checkpoint model, which is what a resumed run
    reads back.
    """

    runtime = SimpleNamespace(
        services=SimpleNamespace(rendering=_RENDERING),
        request=SimpleNamespace(language="ja"),
    )
    state["context"]["context_body_omitted_before_step"] = omit
    with patch.object(turn_input, "local_now_for_model", return_value=now):
        turn = turn_input.build_executing_turn(
            _executing_agent(), state, cast(Any, runtime), tools=()
        )
    prepared = turn.prepare(
        state, rendering=_RENDERING, repair_notice="", provider_turns={}
    )
    recorded = _build_llm_history_entry(
        step_id=f"THINK-{think}",
        step_number=think,
        phase="executing",
        summary="",
        tool_id="read",
        started_at="2026-09-19T00:00:00Z",
        completed_at="2026-09-19T00:00:01Z",
        short_step_id=f"S-{think}-THINK",
        turn_context=prepared.turn_context,
        world_state=prepared.world_state,
    )
    restored = HistoryEntryModel.model_validate(recorded).model_dump(exclude_none=True)
    state["history_by_scope"]["S"].extend(
        [restored, _tool(think, think=think, call_id=call_id)]
    )
    return turn.head, prepared


def _two_runs(second_run: dict[str, str]) -> tuple[list[str], Any, Any, Any]:
    """Run 1 takes two THINKs; a new message starts run 2 with ``second_run``."""

    state = _state([_user(1, "りんごを英語にして")])
    state["context"].update(_RUN_1)
    head_1, _ = _think_once(state, think=2, call_id="c2")
    head_2, last = _think_once(state, think=3, call_id="c3")
    state["history_by_scope"]["S"].append(_user(4, "みかんは？"))
    state["context"].update(second_run)
    head_3, first = _think_once(state, think=5, call_id="c5")
    head_4, after = _think_once(state, think=6, call_id="c6")
    return [head_1, head_2, head_3, head_4], last, first, after


_UPDATE_HEADINGS = (
    "## Workspace Update",
    "## AGENTS.md Update",
    "## Memory Update",
    "## Linkable Persisted Memory Update",
    "Current time: ",
)


def _updates(prepared: Any) -> list[str]:
    context = prepared.turn_context or ""
    return [heading for heading in _UPDATE_HEADINGS if heading in context]


@pytest.mark.parametrize(
    ("second_run", "expected"),
    [
        ({}, []),
        # Memory is read once per Action, so a later run shows no new version.
        ({"insight_data": "I-2"}, []),
        (
            {"agents_md_instructions": "# AGENTS.md instructions\nA-2"},
            ["## AGENTS.md Update"],
        ),
        ({"workspace_context_prompt": "W-2"}, ["## Workspace Update"]),
        (
            {
                "agents_md_instructions": "# AGENTS.md instructions\nA-2",
                "workspace_context_prompt": "W-2",
            },
            ["## Workspace Update", "## AGENTS.md Update"],
        ),
    ],
)
def test_a_new_message_appends_to_the_last_request_and_only_what_changed(
    second_run: dict[str, str], expected: list[str]
) -> None:
    """新しい依頼の最初の要求は、前の依頼の最後の要求を項目単位の接頭辞に持つ。

    The head used to show the latest message and this run's memory, so every
    new message rewrote the request's first item and re-billed the whole
    conversation behind it.
    """

    heads, last, first, after = _two_runs(second_run)

    assert len(set(heads)) == 1
    assert "りんご" not in heads[0] and "みかん" not in heads[0]
    assert first.conversation[: len(last.conversation)] == last.conversation
    assert after.conversation[: len(first.conversation)] == first.conversation
    assert _updates(last) == []
    assert _updates(first) == expected
    # Sent once: the next turn reads it off the row that carried it.
    assert _updates(after) == []
    for field, value in second_run.items():
        if not expected:
            assert first.turn_context is None
            continue
        assert value in first.turn_context
        # Recorded as the head renders it, which is what a later turn compares.
        assert value in first.world_state[field]


def test_a_removed_agents_md_is_withdrawn_once() -> None:
    _, _, first, after = _two_runs({"agents_md_instructions": ""})

    assert "no longer apply" in first.turn_context
    assert first.world_state == {"agents_md_instructions": ""}
    assert _updates(after) == []


@pytest.mark.parametrize(
    ("change", "shown"),
    [
        ({"agents_md_instructions": ""}, "no longer apply"),
        ({"workspace_context_prompt": "W-2"}, "## Workspace Update"),
        ({}, "Current time: T1"),
    ],
)
def test_the_string_fallback_shows_what_changed_since_the_head(
    change: dict[str, str], shown: str
) -> None:
    """記録した turn context を送れない文字列の経路でも、今の状態を示す。"""

    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    _think_once(state, think=2, call_id="c2")
    state["history_by_scope"]["S"].append(_user(3))
    state["context"].update(change)
    _, structured = _think_once(state, think=4, call_id="c2", now="T1")
    # A provider reusing a call id makes the window unpairable, so the turn is
    # sent as one string that replays no recorded turn context.
    _, fallback = _think_once(state, think=5, call_id="c5", now="T1")

    assert structured.conversation is not None
    assert shown in structured.turn_context
    assert fallback.conversation is None
    assert shown in fallback.prompt
    for value in change.values():
        assert value in fallback.prompt
    assert fallback.prompt.index(shown) > fallback.prompt.index("S-3-USER")


# --- 呼び出しごとの turn context は変わったものだけ ------------------------------


def test_a_turn_that_reads_nothing_new_sends_no_turn_context() -> None:
    """何も変わらなければ turn context の項目そのものを足さない。"""

    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    _, first = _think_once(state, think=2, call_id="c2")
    _, second = _think_once(state, think=3, call_id="c3")

    assert first.turn_context is None and second.turn_context is None
    assert second.conversation[: len(first.conversation)] == first.conversation
    # The request ends on the tool's result, which every provider answers.
    assert isinstance(second.conversation[-1], LlmTurnToolResultItem)
    assert not any(
        _text(item).startswith("# Turn Context")
        for item in second.conversation
        if isinstance(item, LlmTurnUserItem)
    )


def test_each_changed_turn_section_is_appended_once() -> None:
    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    _, first = _think_once(state, think=2, call_id="c2", now="T0")
    _, later = _think_once(state, think=3, call_id="c3", now="T1")
    coverage = state["context"]["memory_source_coverage"]
    state["context"]["memory_source_coverage"] = {
        **coverage,
        "evaluated_at": "2026-09-19T01:00:00Z",
    }
    state["context"]["workspace_context_prompt"] = "W-2"
    _, both = _think_once(state, think=4, call_id="c4", now="T2")
    _, again = _think_once(state, think=5, call_id="c5", now="T2")

    assert first.turn_context is None
    assert later.turn_context == TURN_CONTEXT_HEADING + "Current time: T1"
    # Memory source coverage is fixed for the Action, like the memory it covers.
    assert _updates(both) == ["## Workspace Update", "Current time: "]
    assert again.turn_context is None
    for earlier, next_ in ((first, later), (later, both), (both, again)):
        assert next_.conversation[: len(earlier.conversation)] == earlier.conversation


def test_an_assistant_ending_with_nothing_new_gets_a_minimal_turn_context() -> None:
    """assistant で終わる会話は続きの生成と読まれるので、短い user 項目で閉じる。"""

    entries = [_user(1), _think(2, turn_context=None), _commentary(3, "調べます")]
    items = _require(entries, turn_context=None)
    retry = _require(entries, turn_context=None, repair="\nNOTICE")

    assert isinstance(items[-2], LlmTurnAssistantItem)
    assert _text(items[-1]) == UNCHANGED_TURN_CONTEXT
    assert retry[: len(items)] == items


def test_a_head_field_added_after_the_action_started_is_frozen_once() -> None:
    """古いテンプレートで固定した先頭に無い欄も、最初に見た値で固定する。"""

    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    head, _ = _think_once(state, think=2, call_id="c2", now="T0")
    recorded = state["context"]["executing_head_fields"]
    del recorded["current_time"]
    upgraded, _ = _think_once(state, think=3, call_id="c3", now="T5")
    later, prepared = _think_once(state, think=4, call_id="c4", now="T6")

    assert upgraded == later != head
    assert "T5" in later
    assert prepared.turn_context == TURN_CONTEXT_HEADING + "Current time: T6"


def test_a_subagent_is_spawned_with_the_head_the_supervisor_froze() -> None:
    """子は親の先頭をそのまま受け取る。親が後で読み直した値は混ざらない。"""

    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    head, _ = _think_once(state, think=2, call_id="c2", now="T0")
    state["context"]["agents_md_instructions"] = (
        "# AGENTS.md instructions for ~/.pantaray\nA-2"
    )
    later, prepared = _think_once(state, think=3, call_id="c3", now="T1")

    spawned = turn_input.frozen_executing_head(_executing_agent(), state)

    # The parent learns of the new file in its turn context; its head, and so
    # the child's context, stays the one the first turn froze.
    assert _updates(prepared) == ["## AGENTS.md Update", "Current time: "]
    assert spawned == head == later
    assert PANTARAY_DEFAULT_AGENTS_MD in spawned
    assert "# AGENTS.md instructions for ~/.pantaray\nA-1" in spawned
    assert "A-2" not in spawned
    assert "W-1" in spawned and "Current time: T0" in spawned


def test_a_subagent_shares_the_supervisor_rules_after_its_role_section() -> None:
    """役割の節の後ろは親子で同じ文字列。親だけの指示は子に届かない。"""

    config = PromptLoader().load_config("action/executing")
    assert config.system_instruction is not None
    shared = config.system_instruction.partition(turn_input.ROLE_RULES_PLACEHOLDER)[2]
    runtime = SimpleNamespace(
        services=SimpleNamespace(rendering=_RENDERING),
        request=SimpleNamespace(language="ja"),
    )
    parent = turn_input.build_executing_turn(
        _executing_agent(), _state([_user(1)]), cast(Any, runtime), tools=()
    ).system_instruction
    child = subagent_job._subagent_system_instruction()

    # The role leads, so the Supervisor reads its own role first.
    assert parent.startswith("## Your Role\nYou are the Action Agent Supervisor.")
    assert child.startswith("## Your Role\nYou are a subagent of an Action.")
    assert parent.endswith(shared) and child.endswith(shared)
    for rule in (
        "## Tool Use Rules",
        "One turn may request several read-only calls at once",
        "Do not request two changing tools",
        "## Quality of Work",
        "## Checking Results",
        "## AGENTS.md",
    ):
        assert rule in shared
    assert "submit_subagent_report" in child
    assert "Only when you will change files in a repository" in child
    for supervisor_only in (
        "draft_final_answer",
        "submit_final_answer",
        "plan.md",
        "spawn_subagent",
        "wait_subagents",
        "step_note",
        "commentary",
        "history_fetch",
    ):
        assert supervisor_only in parent
        assert supervisor_only not in child
    assert "submit_subagent_report" not in parent


def test_a_rebuilt_window_sends_the_update_it_dropped() -> None:
    """境界より前の turn context は再生されないので、その回に出し直す。"""

    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    _think_once(state, think=2, call_id="c2")
    state["history_by_scope"]["S"].append(_user(3))
    state["context"]["workspace_context_prompt"] = "W-2"
    _, sent = _think_once(state, think=4, call_id="c4")
    _, kept = _think_once(state, think=5, call_id="c5")
    _, rebuilt = _think_once(state, think=6, call_id="c6", omit=5)

    assert _updates(sent) == ["## Workspace Update"]
    assert _updates(kept) == []
    assert _updates(rebuilt) == ["## Workspace Update"]


def test_a_rebuilt_window_sends_nothing_when_the_head_is_current_again() -> None:
    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    _think_once(state, think=2, call_id="c2")
    state["history_by_scope"]["S"].append(_user(3))
    state["context"]["workspace_context_prompt"] = "W-2"
    _think_once(state, think=4, call_id="c4")
    state["history_by_scope"]["S"].append(_user(5))
    state["context"]["workspace_context_prompt"] = "W-1"
    _, back = _think_once(state, think=6, call_id="c6")
    _, rebuilt = _think_once(state, think=7, call_id="c7", omit=6)

    # The update to W-2 is still shown, so returning to W-1 is itself an update;
    # once the window drops it, the head already shows W-1.
    assert _updates(back) == ["## Workspace Update"]
    assert _updates(rebuilt) == []


def test_a_retry_appends_its_notice_behind_the_context_already_sent() -> None:
    entries = [_user(1), _think(2), _tool(2, think=2, call_id="call_a")]
    attempt = _require(entries, turn_context="TC-3")
    retry = _require(entries, turn_context="TC-3", repair="\n\n# System Notice\nfix it")

    assert retry[: len(attempt)] == attempt
    assert _text(retry[-1]) == "\n\n# System Notice\nfix it"


# --- 送るものの組み立てと経路 --------------------------------------------------


def _state(entries: list[Any]) -> Any:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-09-19T00:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["history_by_scope"]["S"] = cast(Any, entries)
    return state


# The head showed T1; this turn reads T2.
_LATER_TIME = WorldState(
    head={"current_time": "T1"},
    current={"current_time": "T2"},
    template=lambda key: "Current time: {current_time}",
)
_LATER_TIME_TEXT = "Current time: T2"


def _prepare(
    entries: list[Any],
    *,
    sends_conversation: bool,
    repair: str = "",
    provider_turns: dict[str, Any] | None = None,
    world_state: WorldState | None = _LATER_TIME,
) -> Any:
    return turn_input.ExecutingTurn(
        head="HEAD\n",
        system_instruction="SYS",
        tool_bytes=0,
        scope_handles=("S",),
        sends_conversation=sends_conversation,
        world_state=world_state,
    ).prepare(
        _state(entries),
        rendering=_RENDERING,
        repair_notice=repair,
        provider_turns=provider_turns or {},
    )


def test_the_input_estimate_is_not_dragged_by_a_replayed_turns_bytes() -> None:
    """encrypted_content は入力トークンにほぼ効かないので、見積もりにも入れない。"""

    entries = [_user(1), _think(2), _tool(2, think=2, call_id="call_a")]
    bare = _prepare(entries, sends_conversation=True)
    replayed = _prepare(
        entries,
        sends_conversation=True,
        provider_turns={
            "THINK-2": _openai_turn(("call_a", "read"), encrypted="x" * 4000)
        },
    )

    assert replayed.conversation is not None
    assert (
        cast(LlmTurnAssistantItem, replayed.conversation[2]).provider_turn is not None
    )
    assert replayed.rendered_bytes == bare.rendered_bytes


def test_the_string_route_sends_the_one_rendering_it_also_records() -> None:
    prepared = _prepare([_user(1)], sends_conversation=False)

    assert prepared.conversation is None
    assert prepared.turn_context is None
    assert prepared.prompt == prepared.recorded_prompt
    assert prepared.recorded_prompt.startswith("HEAD\n")
    assert prepared.recorded_prompt.endswith("\n\n" + _LATER_TIME_TEXT)
    assert "調べてください" in prepared.recorded_prompt


def test_the_conversation_route_sends_the_head_alone_and_still_records_the_string() -> (
    None
):
    entries = [_user(1), _think(2), _tool(2, think=2, call_id="call_a")]
    prepared = _prepare(entries, sends_conversation=True, repair="\nNOTICE")

    assert prepared.prompt == "HEAD\n"
    assert prepared.conversation is not None
    assert "S-2-TOOL" not in prepared.prompt
    # The turn context is what gets recorded; the retry notice is not part of it.
    assert prepared.turn_context == TURN_CONTEXT_HEADING + _LATER_TIME_TEXT
    assert prepared.world_state == {"current_time": "T2"}
    assert _text(prepared.conversation[-2]) == prepared.turn_context
    assert _text(prepared.conversation[-1]) == "\nNOTICE"
    assert prepared.recorded_prompt == (
        "HEAD\n"
        + _RENDERING.format_history(_state(entries))
        + "\n\n"
        + _LATER_TIME_TEXT
        + "\nNOTICE"
    )
    assert prepared.file_inputs == ()


def test_build_executing_turn_splits_at_the_history_and_sends_the_items() -> None:
    template = (
        "Summary: {request_summary}\nCurrent time: {current_time}\n{action_history}"
    )
    agent = cast(
        Any,
        SimpleNamespace(
            executing_prompt=template,
            executing_system_instruction="SYS for {final_answer_language}",
            DEFAULT_SYSTEM_INSTRUCTION="D",
            executing_role_rule=lambda key: "RULE",
            executing_world_state_update=lambda key: "UPDATE",
        ),
    )
    runtime = cast(
        Any,
        SimpleNamespace(
            services=SimpleNamespace(rendering=_RENDERING),
            request=SimpleNamespace(language="ja"),
        ),
    )
    state = _state([_user(1)])

    built = turn_input.build_executing_turn(agent, state, runtime, tools=())

    assert built.head.startswith("Summary: (none)\nCurrent time: ")
    assert built.system_instruction == "SYS for Japanese"
    assert built.sends_conversation is True
    assert (
        built.prepare(
            state, rendering=_RENDERING, repair_notice="", provider_turns={}
        ).conversation
        is not None
    )
    # Without the seam the history goes last and the turn is one string.
    seamless = cast(
        Any, SimpleNamespace(**{**vars(agent), "executing_prompt": "H {current_time}"})
    )
    assert (
        turn_input.build_executing_turn(
            seamless, state, runtime, tools=()
        ).sends_conversation
        is False
    )
    # Anything after the history would be resent on every turn.
    trailing = cast(
        Any,
        SimpleNamespace(**{**vars(agent), "executing_prompt": "H{action_history}T"}),
    )
    with pytest.raises(ValueError, match="must end with"):
        turn_input.build_executing_turn(trailing, state, runtime, tools=())
