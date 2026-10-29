"""The make stage: the selected lens run, its research still in context, makes
what it suggests with tools that write only into the Suggestion's artifact
folder, and ends with ``finish_suggestion``: the decision as it now stands.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pantaray_agents.conversation.loop import Continue, Finish, IdleTurn
from pantaray_agents.conversation.window import ConversationEntry
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.suggestion import (
    SUGGESTION_ARTIFACTS_MAX_COUNT,
    SuggestionArtifacts,
    SuggestionDecidedContent,
    SuggestionExtraction,
    SuggestionInteractionContract,
)
from pantaray_agents.tools.contract import (
    BrokerPolicyError,
    resolve_react_tool_definitions,
)
from pantaray_agents.tools.files.workspace_descriptor_access import (
    WorkspacePathMissingError,
    open_workspace_file_descriptor,
)
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.conversation import LlmTurnToolResultItem
from pantaray_llm.contracts.tool_use import LlmToolDefinition

from .nudges import render_nudge
from .react import (
    SUGGESTION_PYTHON_TOOL_ID,
    SUGGESTION_TOOL_IDS,
    SuggestionConversation,
    SuggestionRunSteps,
    SuggestionStepRecorder,
    offered_tool_ids,
)
from .research import SuggestionResearchTools

SUGGESTION_MAKE_PROMPT_NAME = "suggestion/suggestion_make"
FINISH_SUGGESTION_TOOL_NAME = "finish_suggestion"
MAKE_STAGE_TOOL_IDS = (
    *SUGGESTION_TOOL_IDS,
    "apply_patch",
    SUGGESTION_PYTHON_TOOL_ID,
    "render_html_page",
    "save_html_as_pdf",
)
_FINISH_REQUIRED = (
    "Respond with tool calls. When you are done, call "
    f"`{FINISH_SUGGESTION_TOOL_NAME}` alone with your decision."
)


class _MadeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_suggestion: bool
    interaction_contract: SuggestionInteractionContract | None
    key_point: str = Field(max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)
    artifacts: SuggestionArtifacts


@dataclass(frozen=True, slots=True)
class _Made:
    decided: SuggestionDecidedContent | None  # None drops the Suggestion
    artifacts: list[str]


def _finish_tool() -> LlmToolDefinition:
    return LlmToolDefinition(
        name=FINISH_SUGGESTION_TOOL_NAME,
        description=(
            "Submit the suggestion as it stands now: with the files you made, "
            "unchanged when you made none, or dropped."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "has_suggestion",
                "interaction_contract",
                "key_point",
                "artifacts",
            ],
            "properties": {
                "has_suggestion": {"type": "boolean"},
                "interaction_contract": {
                    "type": ["string", "null"],
                    "enum": ["action_offer", "message_only", None],
                },
                "key_point": {
                    "type": "string",
                    "maxLength": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                },
                "artifacts": {
                    "type": "array",
                    "maxItems": SUGGESTION_ARTIFACTS_MAX_COUNT,
                    "items": {"type": "string"},
                    "description": (
                        "The files to show the user, relative to the artifact "
                        "folder, in the order to show them; empty when none."
                    ),
                },
            },
        },
    )


def _parse_made(arguments: Mapping[str, object], folder: Path) -> _Made:
    output = _MadeOutput.model_validate(arguments)
    key_point = output.key_point.strip()
    contract = output.interaction_contract
    if not output.has_suggestion:
        if key_point or contract is not None or output.artifacts:
            raise ValueError(
                "key_point must be empty, interaction_contract null and "
                "artifacts empty when has_suggestion=false"
            )
        return _Made(None, [])
    if not key_point or contract is None:
        raise ValueError(
            "key_point and interaction_contract are required when has_suggestion=true"
        )
    for path in output.artifacts:
        try:
            os.close(
                open_workspace_file_descriptor(root_path=folder, relative_path=path)
            )
        except (WorkspacePathMissingError, BrokerPolicyError) as exc:
            raise ValueError(f"{path} is not a file in the artifact folder") from exc
    return _Made(
        {"interaction_contract": contract, "key_point": key_point}, output.artifacts
    )


async def make_suggestion_artifacts(
    *,
    user_id: str,
    suggestion_id: str,
    extraction: SuggestionExtraction,
    conversation: SuggestionConversation,
    research_tools: SuggestionResearchTools,
    make_config: PromptConfig,
    record_step: SuggestionStepRecorder,
) -> SuggestionExtraction | None:
    """Continue the selected run to make its files; None when it drops the
    Suggestion.

    The Suggestion job owns the folder: it removes it unless a successful
    Suggestion listing these files is saved.
    """

    submitted = conversation.ending_call
    assert submitted is not None  # The lens run ended on it.
    make_tools = await research_tools.build_make_tools(
        user_id=user_id, run_id=suggestion_id
    )
    folder = make_tools.folder
    instruction = make_config.prompt.strip()

    def decide(turn: IdleTurn) -> Finish[_Made] | Continue:
        if turn.ending_call is None:
            return Continue(_FINISH_REQUIRED)
        try:
            made = _parse_made(turn.ending_call.arguments, folder)
        except (ValidationError, ValueError) as exc:
            return Continue(
                f"{FINISH_SUGGESTION_TOOL_NAME} was rejected: {str(exc)[:1200]}"
            )
        # The run judged its first key point; what it made may read differently.
        notice = (
            None
            if turn.final or made.decided is None
            else conversation.repeat_notice(made.decided["key_point"])
        )
        if notice is not None:
            return Continue(
                render_nudge(
                    conversation.nudge_config, FINISH_SUGGESTION_TOOL_NAME, notice
                )
            )
        return Finish(made)

    made = await conversation.converse(
        tools=resolve_react_tool_definitions(
            definitions=make_tools.definitions,
            tool_ids=offered_tool_ids(make_tools.definitions, MAKE_STAGE_TOOL_IDS),
        ),
        ending_tool=_finish_tool(),
        opening=(
            ConversationEntry(
                LlmTurnToolResultItem(
                    type="tool_result",
                    call_id=submitted.call_id,
                    name=submitted.name,
                    output={"status": "selected", "instruction": instruction},
                )
            ),
        ),
        steps=SuggestionRunSteps(
            run_id=suggestion_id, record=record_step, prompt=instruction
        ),
        decide=decide,
    )
    if made.decided is None:
        return None
    return {
        **extraction,
        "decided": made.decided,
        "interaction_contract": made.decided["interaction_contract"],
        "artifacts": made.artifacts,
    }


__all__ = [
    "FINISH_SUGGESTION_TOOL_NAME",
    "MAKE_STAGE_TOOL_IDS",
    "SUGGESTION_MAKE_PROMPT_NAME",
    "make_suggestion_artifacts",
]
