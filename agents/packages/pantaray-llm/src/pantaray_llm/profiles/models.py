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
    reasoning: Literal["effort", "adaptive", "none"]
    max_output_tokens: int | None


_MULTIMODAL_CAPABILITIES: Mapping[LlmCapability, bool] = {
    "tool_use": True,
    "image_input": True,
    "structured_output": True,
}

# Verified 2026-09-30 against the model/effort documentation, not API aliases
# guessed from names. ChatGPT capabilities come from Codex's models-manager
# catalog and Responses request builder, independently of the public API.
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
# https://developers.openai.com/api/docs/models/gpt-6-luna
# https://platform.claude.com/docs/en/build-with-claude/effort
# https://platform.claude.com/docs/en/build-with-claude/context-windows
# https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json
# https://fireworks.ai/models/fireworks/gpt-oss-120b
MODEL_CATALOG = {
    (entry.provider, entry.model): entry
    for entry in (
        *(
            ModelCatalogEntry(
                "openai", model, _MULTIMODAL_CAPABILITIES, "effort", 128000
            )
            for model in ("gpt-6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6")
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
                "anthropic", model, _MULTIMODAL_CAPABILITIES, "adaptive", 128000
            )
            for model in ("claude-opus-5", "claude-sonnet-5")
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
