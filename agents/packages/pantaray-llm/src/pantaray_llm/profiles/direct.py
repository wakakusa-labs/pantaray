"""Resolve a configured model without credentials, routing, or network I/O."""

from pantaray_llm.contracts.request import LlmRequest
from pantaray_llm.contracts.tool_use import LlmToolUseRequest, OpenAiToolContinuation
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_MODEL_CAPABILITY_UNSUPPORTED,
    LlmProvider,
    ProviderError,
)
from pantaray_llm.profiles.models import MODEL_CATALOG
from pantaray_llm.profiles.purposes import DEFAULT_MAX_OUTPUT_TOKENS, LLM_PURPOSES
from pantaray_llm.providers.anthropic.settings import (
    AnthropicAdaptiveThinking,
    AnthropicLlmProfile,
)
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile

# Design limit: unknown output limits use 8,192 tokens. Raise this for a model
# only after its documented limit or a truncated real task provides evidence.
UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 8192


def resolve_direct_profile(
    *, provider: LlmProvider, model: str, request: LlmRequest
) -> OpenAiLlmProfile | AnthropicLlmProfile:
    purpose = LLM_PURPOSES.get(request.purpose)
    if purpose is None:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="Unknown inference purpose.",
        )
    required = set(purpose.required_capabilities)
    if request.tool_use is not None:
        required.add("tool_use")
    if request.response_format is not None:
        required.add("structured_output")
    for message in request.messages:
        for block in message.content:
            if block.type == "input_image":
                required.add("image_input")
    if isinstance(request.tool_use, LlmToolUseRequest):
        continuation = request.tool_use.continuation
        # Only the OpenAI continuation holds media outside the request messages.
        # An Anthropic continuation replays its own blocks, and the turn that
        # put them there was already checked against this model.
        if isinstance(continuation, OpenAiToolContinuation):
            for slot in continuation.media_slots:
                if slot.projection.state == "inline":
                    required.add("image_input")

    entry = MODEL_CATALOG.get((provider, model))
    if entry is not None:
        for capability in sorted(required):
            if entry.capabilities.get(capability) is False:
                raise ProviderError(
                    status_code=400,
                    code=PROXY_MODEL_CAPABILITY_UNSUPPORTED,
                    message="The configured model does not support this inference requirement.",
                    details={
                        "profile_id": purpose.id,
                        "required_capability": capability,
                        "upstream_provider": provider,
                    },
                )
    max_output_tokens = (
        min(DEFAULT_MAX_OUTPUT_TOKENS, entry.max_output_tokens)
        if entry is not None and entry.max_output_tokens is not None
        else UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
    )
    if provider == "anthropic":
        thinking = None
        if entry is not None and entry.reasoning in (
            "adaptive",
            "adaptive_progress_updates",
        ):
            # Purpose effort is a preference; adaptive models have no none
            # level, so their lowest effort represents that preference.
            effort = purpose.reasoning_effort
            if effort == "none":
                effort = "low"
            thinking = AnthropicAdaptiveThinking(
                effort=effort,
                progress_updates=entry.reasoning == "adaptive_progress_updates",
            )
        return AnthropicLlmProfile(
            provider=provider,
            profile_id=purpose.id,
            model=model,
            max_output_tokens=max_output_tokens,
            thinking=thinking,
            enable_image_inputs=purpose.image_detail is not None,
        )
    return OpenAiLlmProfile(
        provider=provider,
        profile_id=purpose.id,
        model=model,
        # Codex's request contract leaves the output budget to the service.
        max_output_tokens=None if provider == "openai_codex" else max_output_tokens,
        reasoning_effort=(
            purpose.reasoning_effort
            if entry is not None and entry.reasoning == "effort"
            else None
        ),
        image_detail=purpose.image_detail,
    )
