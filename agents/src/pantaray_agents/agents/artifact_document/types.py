from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pantaray_agents.agents.artifact_react import (
    PatchCommitResult,
    ReactLoopPolicy,
    ReactLoopResult,
    ReactStepRecorder,
)
from pantaray_agents.agents.artifact_react.artifact_runtime import (
    ArtifactLlmCaller,
    ArtifactPatchApplier,
    ArtifactPromptBuilder,
)
from pantaray_agents.schema.agent import AgentRequest, AgentResponse
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.tools.contract import ReactToolCall, ReactToolDefinition

type ArtifactCommitPatch = Callable[
    [str, str, str, ReactToolCall, int], Awaitable[PatchCommitResult]
]
type CommitCompletedWithoutPatch = Callable[[str], Awaitable[None]]
type BuildSuccessResponse[RequestT: AgentRequest, ResponseT: AgentResponse] = Callable[
    [RequestT, "ReActAgentRunResult"], Awaitable[ResponseT]
]
type BuildErrorResponse[RequestT: AgentRequest, ResponseT: AgentResponse] = Callable[
    [RequestT, AgentError], Awaitable[ResponseT]
]
type ConsumeLlmThoughts = Callable[[], str | None]


@dataclass(frozen=True)
class ReActAgentRunInput[RequestT: AgentRequest, ResponseT: AgentResponse]:
    run_id: str
    logical_path: str
    base_text: str
    build_prompt: ArtifactPromptBuilder
    call_llm: ArtifactLlmCaller
    apply_patch: ArtifactPatchApplier
    commit_patch: ArtifactCommitPatch
    record_step: ReactStepRecorder
    build_success_response: BuildSuccessResponse[RequestT, ResponseT]
    build_error_response: BuildErrorResponse[RequestT, ResponseT]
    tools: tuple[ReactToolDefinition, ...] = ()
    commit_completed_without_patch: CommitCompletedWithoutPatch | None = None
    consume_llm_thoughts: ConsumeLlmThoughts | None = None
    policy: ReactLoopPolicy | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.logical_path.strip():
            raise ValueError("logical_path must not be empty")


@dataclass(frozen=True)
class ReActAgentRunResult:
    final_text: str
    loop_result: ReactLoopResult


class ReActAgentDefinition[RequestT: AgentRequest, ResponseT: AgentResponse](Protocol):
    agent_name: str
    prompt_name: str
    prompt_version: str
    logical_path: str

    async def prepare(
        self, request: RequestT
    ) -> ReActAgentRunInput[RequestT, ResponseT]: ...

    def error_from_loop_result(self, loop_result: ReactLoopResult) -> AgentError: ...
