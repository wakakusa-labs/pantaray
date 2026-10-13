"""Verified model capabilities; absent entries or capabilities remain unknown."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pantaray_llm.errors import LlmProvider
from pantaray_llm.profiles.purposes import LlmCapability


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    provider: LlmProvider
    model: str
    capabilities: Mapping[LlmCapability, bool]
    # "adaptive_progress_updates": adaptive thinking whose notes between tool
    # calls come back as progress-update thinking blocks, readable only when the
    # request asks for them.
    reasoning: Literal["effort", "adaptive", "adaptive_progress_updates", "none"]
    max_output_tokens: int | None


_MULTIMODAL_CAPABILITIES: Mapping[LlmCapability, bool] = {
    "tool_use": True,
    "image_input": True,
    "structured_output": True,
}

# Verified against the model/effort documentation, not API aliases guessed
# from names (OpenAI 2026-09-30, Anthropic 2026-10-08). ChatGPT capabilities
# come from Codex's models-manager catalog and Responses request builder,
# independently of the public API.
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
# https://developers.openai.com/api/docs/models/gpt-6-luna
# https://developers.openai.com/api/docs/models/gpt-6.1-sol (2026-10-13)
# https://platform.claude.com/docs/en/about-claude/models/overview
# https://platform.claude.com/docs/en/build-with-claude/effort
# https://platform.claude.com/docs/en/build-with-claude/structured-outputs
# https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json
# https://fireworks.ai/models/fireworks/gpt-oss-120b
MODEL_CATALOG = {
    (entry.provider, entry.model): entry
    for entry in (
        *(
            ModelCatalogEntry(
                "openai", model, _MULTIMODAL_CAPABILITIES, "effort", 128000
            )
            for model in (
                "gpt-6.1-sol",
                "gpt-6-luna",
                "gpt-5.6-terra",
                "gpt-5.6-sol",
                "gpt-5.6",
            )
        ),
        *(
            ModelCatalogEntry(
                "openai_codex", model, _MULTIMODAL_CAPABILITIES, "effort", None
            )
            for model in (
                "gpt-6-luna",
                "gpt-6-astra",
                "gpt-6.1-sol",
                "gpt-6-sol",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
            )
        ),
        *(
            ModelCatalogEntry(
                "anthropic",
                model,
                _MULTIMODAL_CAPABILITIES,
                "adaptive_progress_updates",
                128000,
            )
            # The models that accept `thinking.display: "updates"` (beta
            # thinking-display-updates-2026-08-18); any other model rejects it.
            for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")
        ),
        *(
            ModelCatalogEntry(
                "anthropic", model, _MULTIMODAL_CAPABILITIES, "adaptive", 128000
            )
            for model in (
                "claude-haiku-5-5",
                # No longer offered in settings, but still served, and a saved
                # connection may name them.
                "claude-opus-5",
                "claude-sonnet-5",
            )
        ),
        ModelCatalogEntry(
            "fireworks",
            "accounts/fireworks/models/gpt-oss-120b",
            {"tool_use": True, "image_input": False, "structured_output": True},
            "none",
            None,
        ),
    )
}
