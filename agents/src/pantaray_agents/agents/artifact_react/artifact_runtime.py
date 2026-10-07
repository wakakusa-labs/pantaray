from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pantaray_agents.tools.contract import ReactToolDefinition, ReactToolRegistry

from .artifact_patch import parse_artifact_document_react_output
from .artifact_patch_tool import (
    ArtifactCommitter,
    ArtifactPatchApplier,
    ArtifactPatchState,
    ArtifactPatchTool,
)
from .response_schema import validate_extra_react_tool_names
from .runner import ReactLlmOutput, ReactStepRecorder, run_react_loop
from .transcript import build_prompt_with_transcript
from .types import (
    ReactLoopPolicy,
    ReactLoopResult,
)

type ArtifactPromptBuilder = Callable[[str, str | None], str]
type ArtifactLlmCaller = Callable[[str], Awaitable[ReactLlmOutput]]
type ArtifactThoughtConsumer = Callable[[], str | None]


@dataclass(frozen=True)
class ArtifactReactExecution:
    final_text: str
    loop_result: ReactLoopResult


async def run_artifact_update_react_loop(
    *,
    run_id: str,
    base_text: str,
    build_prompt: ArtifactPromptBuilder,
    call_llm: ArtifactLlmCaller,
    apply_patch: ArtifactPatchApplier,
    commit_patch: ArtifactCommitter,
    record_step: ReactStepRecorder,
    logical_path: str,
    policy: ReactLoopPolicy | None = None,
    tools: tuple[ReactToolDefinition, ...] = (),
    consume_llm_thoughts: ArtifactThoughtConsumer | None = None,
) -> ArtifactReactExecution:
    validate_extra_react_tool_names(tools)
    state = ArtifactPatchState(current_text=base_text)
    artifact_patch_tool = ArtifactPatchTool(
        state=state,
        apply_patch=apply_patch,
        commit_patch=commit_patch,
        logical_path=logical_path,
    )
    tool_registry = ReactToolRegistry(
        (
            artifact_patch_tool.definition(),
            *tools,
        )
    )

    loop_result = await run_react_loop(
        run_id=run_id,
        initial_prompt="",
        build_prompt=lambda tool_results, last_error: build_prompt_with_transcript(
            initial_prompt=build_prompt(state.current_text, last_error),
            tool_results=tool_results,
            last_error=None,
        ),
        call_llm=call_llm,
        parse_output=parse_artifact_document_react_output,
        execute_tool=tool_registry.execute,
        record_step=record_step,
        policy=policy,
        consume_llm_thoughts=consume_llm_thoughts,
    )
    return ArtifactReactExecution(
        final_text=state.current_text, loop_result=loop_result
    )
