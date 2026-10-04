"""suggestion agent"""

import logging
from datetime import UTC, datetime
from typing import Literal, TypedDict

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.agents.capability_envelopes import (
    ACTION_AGENT_CAPABILITY_ENVELOPE,
)
from pantaray_agents.agents.core import BaseAgent, CountingSink
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.suggestion_agent.context_density import (
    CONTEXT_DENSITY_LOW_MAX_PRESENT as DENSITY_LOW_MAX_PRESENT,
)
from pantaray_agents.agents.suggestion_agent.context_density import (
    CONTEXT_DENSITY_MEDIUM_MAX_PRESENT as DENSITY_MEDIUM_MAX_PRESENT,
)
from pantaray_agents.agents.suggestion_agent.context_density import (
    CONTEXT_DENSITY_SLOT_ORDER as DENSITY_SLOT_ORDER,
)
from pantaray_agents.agents.suggestion_agent.context_density import (
    build_context_density_signal,
    build_context_density_slot_status,
    build_recent_activity_summaries_24h_1w_1m,
    classify_context_density,
)
from pantaray_agents.agents.suggestion_agent.context_fetch import (
    fetch_summary_rows_by_type,
)
from pantaray_agents.agents.suggestion_agent.context_formatters import (
    format_activity_descriptions,
    format_activity_summary,
    format_recent_suggestions,
    normalize_recent_suggestion_entry,
)
from pantaray_agents.agents.suggestion_agent.context_types import (
    ActivityDescriptionRow,
    ActivitySummaryRow,
    SuggestionFetchedContext,
    SuggestionStableMemoryContext,
    normalize_activity_description_rows,
    normalize_activity_summary_rows,
)
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.agents.suggestion_agent.react import run_suggestion_react
from pantaray_agents.agents.suggestion_agent.research import SuggestionResearchTools
from pantaray_agents.agents.suggestion_agent.writer import (
    SUGGESTION_WRITER_PROMPT_NAME,
    write_suggestion_answer,
    writer_voice_instruction,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.repositories.runtime_ports import (
    SuggestionRepositoryPort,
)
from pantaray_agents.schema.agent.base import (
    AgentError,
    AgentRequest,
    JSONValue,
    StatusType,
)
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentRequest,
    SuggestionAgentResponse,
    SuggestionExtraction,
    SuggestionHistoryEntry,
)
from pantaray_agents.schema.repository_errors import repository_data_or_raise
from pantaray_agents.utils.local_time import describe_local_time, local_zone_name
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.tool_use import (
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)
from pantaray_llm.profiles import SUGGESTION_PROFILE_ID

logger = logging.getLogger(__name__)

type SuggestionAgentConfig = dict[str, JSONValue]
type SuggestionLlmPayload = dict[str, JSONValue]

# Design limit: the earlier 64k budget for the rest of the prompt plus insights/todos.md
# up to the snapshot's 60k-character bound. Memory keeps that file to the user's own
# open work, but files written before that rule reached 40k characters. If production
# todos.md exceeds about 20 KB after Memory has run on it, revisit the Memory rules
# rather than raising these limits.
SUGGESTION_INITIAL_PROMPT_MAX_CHARS = 124_000


class SuggestionPersistencePayload(TypedDict):
    prompt_name: str
    prompt_version: str
    prompt_text: str
    response_text: str
    request_images_count: int
    used_images_count: int


class SuggestionAgent(BaseAgent[SuggestionAgentResponse]):
    """ユーザーの行動分析に基づいて次のアクションを提案するエージェント"""

    llm_config: dict[str, object]
    LLM_INFERENCE_PROFILE_ID = SUGGESTION_PROFILE_ID
    CONTEXT_DENSITY_LOW_MAX_PRESENT = DENSITY_LOW_MAX_PRESENT
    CONTEXT_DENSITY_MEDIUM_MAX_PRESENT = DENSITY_MEDIUM_MAX_PRESENT
    CONTEXT_DENSITY_SLOT_ORDER: tuple[tuple[str, str], ...] = DENSITY_SLOT_ORDER
    ANSWER_LANGUAGE_LABELS = {
        "en": "English",
        "ja": "Japanese",
    }

    def __init__(
        self,
        config: SuggestionAgentConfig,
        repository: SuggestionRepositoryPort | None = None,
        research_tools: SuggestionResearchTools | None = None,
        stable_memory: SuggestionStableMemoryContext | None = None,
    ) -> None:
        super().__init__(config=config)
        if repository is None:
            raise ValueError("SuggestionAgent には repository が必須です")
        if research_tools is None:
            raise ValueError("SuggestionAgent には research_tools が必須です")
        if stable_memory is None:
            raise ValueError("SuggestionAgent には stable_memory が必須です")
        self.repository = repository
        self.research_tools = research_tools
        self.stable_memory = stable_memory
        # プロンプト設定のロード（system_instruction と prompt を分離）
        # プロンプトファイルは `prompts/suggestion/suggestion.yaml` に配置する。
        self._prompt_config: PromptConfig = self._load_prompt_config(
            "suggestion/suggestion"
        )
        self._writer_prompt_config = self._load_prompt_config(
            SUGGESTION_WRITER_PROMPT_NAME
        )
        self._last_step_number = 0  # the run's latest recorded step
        self._action_agent_capabilities_prompt_text = ACTION_AGENT_CAPABILITY_ENVELOPE
        self._current_user_id = ""
        self._current_suggestion_id = ""
        self._last_prompt_text = ""
        self._last_response_text = ""

    @property
    def task_suggestion_prompt(self) -> str:
        """後方互換性のためのプロパティ（prompt部分のみ返す）"""
        return self._prompt_config.prompt

    @property
    def system_instruction(self) -> str | None:
        """system_instruction を返す"""
        return self._prompt_config.system_instruction

    def get_response_class(self) -> type[SuggestionAgentResponse]:
        """レスポンスクラスを返す"""
        return SuggestionAgentResponse

    def get_error_code_prefix(self) -> str:
        """エラーコードの接頭辞を返す"""
        return "SUGGESTION"

    def _answer_language_label(self) -> str:
        """Return the prompt-ready language label for `answer` output."""

        lang = self._normalize_language(getattr(self, "_current_language", None))
        return self.ANSWER_LANGUAGE_LABELS[lang]

    def _system_instruction_for_request(self) -> str:
        """Render request-scoped placeholders in the YAML system instruction."""

        base_instruction = self.system_instruction or self.DEFAULT_SYSTEM_INSTRUCTION
        return base_instruction.replace(
            "{answer_language}",
            self._answer_language_label(),
        )

    def _format_activity_descriptions(
        self, rows: list[ActivityDescriptionRow] | None
    ) -> str:
        """直近のActivity Descriptionログをプロンプト用に整形（新しい順）。"""
        return format_activity_descriptions(rows)

    def _format_activity_summary(self, rows: list[ActivitySummaryRow] | None) -> str:
        """直近1時間サマリを1件だけ整形。"""
        return format_activity_summary(rows)

    def _get_reference_time(self) -> datetime:
        """コンテキスト構築の基準時刻（UTC）を返す。

        Note:
            テストで固定時刻を注入できるようにメソッド化している。
        """

        return datetime.now(UTC)

    # BaseAgentの抽象メソッドを実装

    async def _validate_request(self, request: AgentRequest) -> AgentRequest:
        """リクエストの検証と型変換を行う"""
        if not isinstance(request, SuggestionAgentRequest):
            if hasattr(request, "model_dump"):
                # Pydanticモデルの場合は変換を試みる
                validated = SuggestionAgentRequest(**request.model_dump())
                self._current_user_id = validated.user_id
                self._current_suggestion_id = validated.suggestion_id
                return validated
            raise TypeError("Request must be of type SuggestionAgentRequest")
        self._current_user_id = request.user_id
        self._current_suggestion_id = request.suggestion_id
        return request

    async def _fetch_context_data(
        self, request: AgentRequest
    ) -> SuggestionFetchedContext:
        """コンテキストデータを取得する"""
        if not isinstance(request, SuggestionAgentRequest):
            raise TypeError("Request must be of type SuggestionAgentRequest")
        suggestion_request = request

        # 直近のanswer（最新5件）を取得（thinkingは含めない）
        # 期間は広め（30日）に設定し、limitで5件に絞る
        recent_suggestion_rows = repository_data_or_raise(
            await self.repository.get_recent_suggestions(
                suggestion_request.user_id, days=30, limit=5
            ),
            safe_message="failed to fetch recent suggestions",
        )
        recent_entries: list[SuggestionHistoryEntry] = []
        for row in recent_suggestion_rows or []:
            normalized = normalize_recent_suggestion_entry(row)
            if normalized is not None:
                recent_entries.append(normalized)
        recent_suggestions_text = format_recent_suggestions(recent_entries)

        # 直近のActivity Descriptionを3件取得し、実際の対象期間を表示する。
        recent_activity_description_rows = repository_data_or_raise(
            await self.repository.get_recent_activity_descriptions(
                suggestion_request.user_id, limit=3
            ),
            safe_message="failed to fetch recent activity descriptions",
        )
        activity_desc_rows = normalize_activity_description_rows(
            recent_activity_description_rows
        )
        activity_desc_text = self._format_activity_descriptions(activity_desc_rows)

        # 直近1時間サマリ（1件）
        recent_summary_1h_rows = repository_data_or_raise(
            await self.repository.get_recent_activity_summary_1h(
                suggestion_request.user_id, limit=1
            ),
            safe_message="failed to fetch recent 1h activity summary",
        )
        summary_1h_rows = normalize_activity_summary_rows(recent_summary_1h_rows)
        activity_summary_text = self._format_activity_summary(summary_1h_rows)
        summary_1h_row = summary_1h_rows[0] if summary_1h_rows else None

        # 追加: Activity Summary（24h/1w/1m）を取得し、ended_ago 付きで整形する
        reference_time = self._get_reference_time()
        rows_by_type = await fetch_summary_rows_by_type(
            repository=self.repository,
            user_id=suggestion_request.user_id,
        )

        activity_summaries_24h_1w_1m = build_recent_activity_summaries_24h_1w_1m(
            reference_time=reference_time,
            rows_by_type=rows_by_type,
        )
        slot_status = build_context_density_slot_status(
            has_long_term_insight=self.stable_memory.has_insights,
            has_long_term_facts=self.stable_memory.has_facts,
            activity_desc_rows=activity_desc_rows,
            summary_1h_row=summary_1h_row,
            rows_by_type=rows_by_type,
        )
        present_count = sum(1 for is_present in slot_status.values() if is_present)
        density = classify_context_density(present_count)
        context_density_signal = build_context_density_signal(
            density=density,
            present_count=present_count,
            slot_status=slot_status,
        )

        return {
            "short_term_insight": suggestion_request.short_term_insight,
            "reconsideration_reason": suggestion_request.reconsideration_reason,
            "stable_memory_context": self.stable_memory.prompt,
            "action_agent_capabilities": (self._action_agent_capabilities_prompt_text),
            "recent_suggestions": recent_suggestions_text,
            "recent_activity_descriptions": activity_desc_text,
            "recent_activity_summary_1h": activity_summary_text,
            "recent_activity_summaries_24h_1w_1m": activity_summaries_24h_1w_1m,
            "context_density_signal": context_density_signal,
            "workspace_context_prompt": suggestion_request.workspace_context_prompt
            or "",
        }

    def _build_prompt(self, context_data: SuggestionFetchedContext) -> str:
        """プロンプトを構築する"""
        reference_time = self._get_reference_time()
        values: dict[str, str] = {
            "current_time": describe_local_time(reference_time, local_zone_name()),
            "short_term_insight": context_data["short_term_insight"],
            "reconsideration_reason": context_data["reconsideration_reason"],
            "stable_memory_context": context_data["stable_memory_context"],
            "pending_work_context": self.stable_memory.pending_work.strip()
            or "(No pending work recorded.)",
            "action_agent_capabilities": context_data["action_agent_capabilities"],
            "recent_suggestions": context_data["recent_suggestions"],
            "recent_activity_descriptions": context_data[
                "recent_activity_descriptions"
            ],
            "recent_activity_summary_1h": context_data["recent_activity_summary_1h"],
            "recent_activity_summaries_24h_1w_1m": context_data[
                "recent_activity_summaries_24h_1w_1m"
            ],
            "context_density_signal": context_data["context_density_signal"],
            "workspace_context_prompt": context_data["workspace_context_prompt"],
            "answer_language": self._answer_language_label(),
        }
        rendered = self.task_suggestion_prompt.format(**values)
        if len(rendered) > SUGGESTION_INITIAL_PROMPT_MAX_CHARS:
            raise ValueError(
                f"Suggestion initial prompt has {len(rendered)} characters, exceeding "
                f"the {SUGGESTION_INITIAL_PROMPT_MAX_CHARS}-character budget"
            )
        return rendered

    async def _process_llm_response(self, prompt: str) -> SuggestionExtraction:
        """根拠探索を含むbounded ReActで提案を生成する。"""
        if not self._current_user_id or not self._current_suggestion_id:
            raise RuntimeError("Suggestion request scope is not initialized")
        sink = CountingSink()
        self._last_step_number = 0

        async def generate_tool_call(
            *,
            prompt: str,
            tools: tuple[LlmToolDefinition, ...],
            continuation_mode: Literal["disabled", "stateless"],
            continuation: LlmToolContinuation | None,
            tool_result: LlmToolResult | None,
            system_instruction: str,
            stage: str,
        ) -> LlmToolCallTurn:
            return await self._generate_llm_tool_call(
                sink=sink,
                prompt=prompt,
                tools=tools,
                continuation_mode=continuation_mode,
                continuation=continuation,
                tool_result=tool_result,
                system_instruction=system_instruction,
                stage=stage,
            )

        extracted = await run_suggestion_react(
            user_id=self._current_user_id,
            suggestion_id=self._current_suggestion_id,
            initial_prompt=prompt,
            system_instruction=self._system_instruction_for_request(),
            research_tools=self.research_tools,
            generate_tool_call=generate_tool_call,
            parse_output=parse_suggestion_output,
            record_step=self._record_react_step,
            discard_llm_thoughts=self._consume_llm_thoughts,
        )
        decided = extracted["decided"]
        if decided is not None:

            async def generate_text(
                *, prompt: str, system_instruction: str, stage: str
            ) -> str:
                text = await self._generate_llm_response(
                    prompt,
                    sink=sink,
                    system_instruction=system_instruction,
                    stage=stage,
                )
                if not isinstance(text, str):
                    raise RuntimeError("Suggestion writer returned a non-text response")
                return text

            answer_language = self._answer_language_label()
            extracted["answer"] = await write_suggestion_answer(
                run_id=self._current_suggestion_id,
                step_number=self._last_step_number + 1,
                decided=decided,
                answer_language=answer_language,
                config=self._writer_prompt_config,
                voice_instruction=writer_voice_instruction(
                    answer_language, self._load_prompt_config
                ),
                generate_text=generate_text,
                record_step=self._record_react_step,
            )
        self._last_prompt_text = prompt
        self._last_response_text = extracted["response_text"]
        return extracted

    async def _record_react_step(self, step: ReactLoopStep) -> None:
        self._last_step_number = max(self._last_step_number, step.step_number)
        result = await self.repository.save_suggestion_run_step(
            suggestion_id=self._current_suggestion_id,
            step_number=step.step_number,
            step_kind=step.step_kind,
            status=step.status,
            llm_prompt_text=step.prompt_text,
            llm_response_text=step.response_text,
            tool_name=step.tool_name,
            tool_call_envelope=step.tool_call_envelope,
            tool_output=step.tool_output,
            error_code="SUGGESTION_REACT_STEP_ERROR"
            if step.status == "error"
            else None,
            error_message=step.error_message,
        )
        if result.error:
            raise RuntimeError(result.error)

    def _create_success_response(
        self, request: AgentRequest, extracted_data: SuggestionExtraction
    ) -> SuggestionAgentResponse:
        """成功レスポンスを作成する"""
        if not isinstance(request, SuggestionAgentRequest):
            raise TypeError("Request must be of type SuggestionAgentRequest")
        suggestion_request = request

        return SuggestionAgentResponse(
            suggestion_id=suggestion_request.suggestion_id,
            answer=extracted_data["answer"],
            thinking=extracted_data["thinking"],
            suggestion_summary=extracted_data["suggestion_summary"],
            target_context=extracted_data["target_context"],
            created_at=now_utc_iso(),
            has_suggestion=extracted_data["has_suggestion"],
            interaction_contract=extracted_data["interaction_contract"],
            user_id=suggestion_request.user_id,
            status=StatusType.SUCCESS,
            error=None,
        )

    def build_persistence_payload(
        self, response: SuggestionAgentResponse
    ) -> SuggestionPersistencePayload:
        """Runtime/service 側での terminal persistence に必要な監査値を返す。"""
        return {
            "prompt_name": "suggestion",
            "prompt_version": "react_v3",
            "prompt_text": self._last_prompt_text,
            "response_text": self._last_response_text or response.answer,
            "request_images_count": 0,
            "used_images_count": 0,
        }

    async def _save_response(self, response: SuggestionAgentResponse) -> None:
        del response
        return None

    def _get_id_attrs(self) -> dict[str, str]:
        """ID属性名とレスポンスパラメータ名の辞書を返す"""
        return {
            "suggestion_id": "suggestion_id",
            "user_id": "user_id",
        }

    async def _handle_agent_error(
        self, error: AgentError, response_params: dict[str, object]
    ) -> SuggestionAgentResponse:
        """エラーを処理し、terminal persistence は runtime/service 側へ委譲する。"""
        # 基本的なレスポンスパラメータを設定
        user_id = response_params.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id はエラー処理に必須です")
        return self._create_error_response(
            error,
            {
                "suggestion_id": response_params.get("suggestion_id", "error_id"),
                "answer": "",
                "thinking": None,
                "suggestion_summary": None,
                "target_context": None,
                "created_at": now_utc_iso(),
                "has_suggestion": False,
                "interaction_contract": None,
                "user_id": user_id,
            },
        )
