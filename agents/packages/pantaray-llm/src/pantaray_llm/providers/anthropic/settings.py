from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AnthropicAdaptiveThinking:
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    # Whether the model returns its notes between tool calls as progress-update
    # thinking blocks, which an Action turn asks to read as commentary.
    progress_updates: bool = False


@dataclass(frozen=True, slots=True)
class AnthropicLlmProfile:
    provider: Literal["anthropic"]
    profile_id: str
    model: str
    max_output_tokens: int
    # None sends neither thinking nor effort, so a model outside the catalog
    # runs on its own defaults instead of a setting it may reject.
    thinking: AnthropicAdaptiveThinking | None = None
    enable_image_inputs: bool = False
