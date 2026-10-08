from __future__ import annotations

from dataclasses import dataclass

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink
from pantaray_agents.agents.core.tool_llm_runner import ToolLlmRunner
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.agents.memory_file_editor.tools import (
    APPLY_PATCH_TOOL_NAME,
    DELETE_MEMORY_FILE_TOOL_NAME,
    LINK_MEMORY_TOOL_NAME,
    MOVE_MEMORY_FILE_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
    UNLINK_MEMORY_TOOL_NAME,
    WRITE_FILE_TOOL_NAME,
)
from pantaray_agents.conversation.loop import ConversationRequest
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.tooling.agent_experience import (
    HISTORY_FETCH_TOOL_NAME,
    LIST_ACTION_STEPS_TOOL_NAME,
    SEARCH_ACTION_STEPS_TOOL_NAME,
)
from pantaray_agents.schema.agent.memory_update import MemoryUpdateContext
from pantaray_agents.tools.contract import (
    ReactToolDefinition,
    resolve_react_tool_definitions,
)
from pantaray_agents.tools.memory.retrieval import (
    GET_MEMORY_REFERENCE_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
)
from pantaray_agents.utils.profile_brief import (
    build_facts_profile_brief_prompt,
    build_insight_profile_brief_prompt,
    generate_profile_brief_with_retry,
)
from pantaray_agents.utils.prompt_loader import PromptConfig, prompt_loader
from pantaray_llm.profiles import MEMORY_UPDATE_PROFILE_ID

# One run may touch three memory categories, and each edit costs a read of the
# target file first. The Agent Experience run needed 10 turns / 16 tool calls for
# a single category, so this starts at roughly three times that budget. No
# measurement exists yet; tighten it once real runs report their tool-call counts.
# Writing an endeavor's big picture searches its history first, which doubles it.
MEMORY_UPDATE_MAX_LLM_TURNS = 60
MEMORY_UPDATE_MAX_TOOL_CALLS = 96


@dataclass(frozen=True, slots=True)
class MemoryUpdateAgentResult:
    applied_memory_request_ids: tuple[str, ...]


class MemoryUpdateAgent(LlmToolUseMixin, ToolLlmRunner):
    """ReAct editor for one user's Fact, Insight and Agent Experience memory."""

    PROMPT_NAME = "memory_update"
    PROMPT_VERSION = "1.5"
    TOOL_IDS = (
        READ_FILE_TOOL_NAME,
        SEARCH_FILES_TOOL_NAME,
        APPLY_PATCH_TOOL_NAME,
        WRITE_FILE_TOOL_NAME,
        LINK_MEMORY_TOOL_NAME,
        UNLINK_MEMORY_TOOL_NAME,
        MOVE_MEMORY_FILE_TOOL_NAME,
        DELETE_MEMORY_FILE_TOOL_NAME,
        MEMORY_SEARCH_TOOL_NAME,
        GET_MEMORY_REFERENCE_TOOL_NAME,
        LIST_ACTION_STEPS_TOOL_NAME,
        SEARCH_ACTION_STEPS_TOOL_NAME,
        HISTORY_FETCH_TOOL_NAME,
    )

    def __init__(
        self,
        *,
        client: object,
        llm_config: dict[str, object],
    ) -> None:
        self._prompt_config: PromptConfig = prompt_loader.load_config(self.PROMPT_NAME)
        super().__init__(
            client=client,
            llm_config=llm_config,
            default_system_instruction=self._prompt_config.system_instruction or "",
            error_code_prefix="MEMORY_UPDATE",
            llm_inference_profile_id=MEMORY_UPDATE_PROFILE_ID,
        )

    async def update(
        self,
        context: MemoryUpdateContext,
        *,
        tool_definitions: tuple[ReactToolDefinition, ...],
        tool_result_directory_fd: int,
    ) -> MemoryUpdateAgentResult:
        selected_tools = resolve_react_tool_definitions(
            definitions=tool_definitions,
            tool_ids=self._selected_tool_ids(tool_definitions),
        )
        initial_prompt = self._prompt_config.prompt.format(
            short_term_insights=context.short_term_insights or "- none",
            activity_summaries=context.activity_summaries or "- none",
            action_turns=context.action_turns or "- none",
            memory_requests=context.memory_requests or "- none",
            session_memories=context.session_memories or "- none",
            local_time_note=context.local_time_note,
            memory_file_manifest=context.memory_file_manifest,
            workspace_context_prompt=context.workspace_context_prompt or "- none",
            new_experience_ids="\n".join(
                f"- {item}" for item in context.new_experience_ids
            )
            or "- none",
            draft_revision=context.draft_revision,
        )
        # The prompt's last paragraph leads the conversation, which a request
        # needs at least one item of; the rest is the request's own message.
        head, _, task = initial_prompt.rstrip().rpartition("\n\n")
        sink = CountingSink()

        async def send(request: ConversationRequest) -> ActionTurnReply:
            return await self._generate_llm_action_turn(
                sink=sink,
                prompt=request.prompt,
                tools=request.tools,
                max_parallel_tool_calls=request.max_parallel_tool_calls,
                system_instruction=request.system_instruction,
                conversation=request.conversation,
                stage="memory_update",
            )

        applied = await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id=context.run_id,
                tool_result_directory_fd=tool_result_directory_fd,
                tool_definitions=selected_tools,
                prompt=head,
                task=task,
                system_instruction=self._prompt_config.system_instruction or "",
                send=send,
                usage=lambda: sink.delta,
                max_turns=MEMORY_UPDATE_MAX_LLM_TURNS,
                max_tool_calls=MEMORY_UPDATE_MAX_TOOL_CALLS,
                memory_request_ids=context.memory_request_ids,
            )
        )
        return MemoryUpdateAgentResult(applied_memory_request_ids=applied)

    async def generate_profile_brief(
        self, source: MemorySource, memory_text: str
    ) -> str:
        """The compact brief downstream agents read instead of the whole tree."""

        prompt = (
            build_facts_profile_brief_prompt(memory_text)
            if source == "fact"
            else build_insight_profile_brief_prompt(memory_text)
        )
        sink = CountingSink()

        async def call_llm(profile_prompt: str) -> str:
            response = await self._generate_llm_response(
                prompt=profile_prompt,
                sink=sink,
                system_instruction=self._prompt_config.system_instruction,
                stage="memory_profile_brief",
            )
            return response if isinstance(response, str) else str(response)

        return await generate_profile_brief_with_retry(prompt=prompt, call_llm=call_llm)

    def _selected_tool_ids(
        self, tool_definitions: tuple[ReactToolDefinition, ...]
    ) -> tuple[str, ...]:
        # A run without an Action terminal has no Action history to read, so its
        # history tools are absent rather than empty.
        available = {definition.name for definition in tool_definitions}
        return tuple(item for item in self.TOOL_IDS if item in available)


__all__ = [
    "MEMORY_UPDATE_MAX_LLM_TURNS",
    "MEMORY_UPDATE_MAX_TOOL_CALLS",
    "MemoryUpdateAgent",
    "MemoryUpdateAgentResult",
]
