from __future__ import annotations

from dataclasses import replace

import pytest

from pantaray_llm.contracts.request import LlmRequest
from pantaray_llm.errors import LlmProvider, ProviderError
from pantaray_llm.profiles.direct import resolve_direct_profile
from pantaray_llm.profiles.models import MODEL_CATALOG
from pantaray_llm.providers.anthropic.settings import (
    AnthropicAdaptiveThinking,
    AnthropicLlmProfile,
)
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile

_FIREWORKS_MODEL = "accounts/fireworks/models/gpt-oss-120b"
_DESCRIPTOR = {
    "blob_ref": "image-1",
    "mime_type": "image/png",
    "byte_size": 10,
    "sha256": "a" * 64,
}


def _request(*, purpose: str = "action.executing", image: bool = False) -> LlmRequest:
    content = [{"type": "input_text", "text": "Read the input."}]
    if image:
        content.append({"type": "input_image", "image": _DESCRIPTOR})
    return LlmRequest.model_validate(
        {
            "purpose": purpose,
            "trace": {"local_job_id": "job-1"},
            "messages": [{"role": "user", "content": content}],
        }
    )


@pytest.mark.parametrize(
    ("provider", "model", "effort", "output_limit"),
    [
        ("openai", "gpt-6-luna", "high", 65536),
        ("openai", "gpt-5.6-terra", "high", 65536),
        ("openai", "gpt-5.6-sol", "high", 65536),
        ("openai", "gpt-5.6", "high", 65536),
        ("openai_codex", "gpt-6-luna", "high", None),
        ("openai_codex", "gpt-6-astra", "high", None),
        ("openai_codex", "gpt-6.1-sol", "high", None),
        ("openai_codex", "gpt-6-sol", "high", None),
        ("openai_codex", "gpt-5.6-luna", "high", None),
        ("openai_codex", "gpt-5.5", "high", None),
        ("fireworks", _FIREWORKS_MODEL, None, 8192),
    ],
)
def test_selected_responses_model_controls_provider_parameters(
    provider: LlmProvider, model: str, effort: str | None, output_limit: int | None
) -> None:
    profile = resolve_direct_profile(provider=provider, model=model, request=_request())
    assert isinstance(profile, OpenAiLlmProfile)
    assert profile.provider == provider
    assert profile.model == model
    assert profile.reasoning_effort == effort
    assert profile.max_output_tokens == output_limit


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5"])
@pytest.mark.parametrize(
    ("purpose", "effort"),
    [("action.executing", "high"), ("activity_summary", "medium")],
)
def test_anthropic_uses_the_purpose_effort_with_adaptive_thinking(
    model: str, purpose: str, effort: str
) -> None:
    profile = resolve_direct_profile(
        provider="anthropic", model=model, request=_request(purpose=purpose)
    )
    assert isinstance(profile, AnthropicLlmProfile)
    assert isinstance(profile.thinking, AnthropicAdaptiveThinking)
    assert profile.thinking.effort == effort
    assert profile.max_output_tokens == 65536


@pytest.mark.parametrize(
    "provider", ["openai", "openai_codex", "anthropic", "fireworks"]
)
def test_custom_models_keep_the_configured_name_without_guessing_reasoning(
    provider: LlmProvider,
) -> None:
    model = "my-custom/model"
    profile = resolve_direct_profile(
        provider=provider, model=model, request=_request(image=True)
    )
    assert profile.model == model
    if isinstance(profile, AnthropicLlmProfile):
        assert profile.thinking is None
    else:
        assert profile.reasoning_effort is None
    assert profile.max_output_tokens == (None if provider == "openai_codex" else 8192)


def test_public_api_alias_does_not_inherit_chatgpt_model_metadata() -> None:
    profile = resolve_direct_profile(
        provider="openai_codex", model="gpt-5.6", request=_request()
    )
    assert isinstance(profile, OpenAiLlmProfile)
    assert profile.reasoning_effort is None
    assert profile.model == "gpt-5.6"


def test_text_only_model_rejects_actual_image_input_with_actionable_details() -> None:
    with pytest.raises(ProviderError) as error:
        resolve_direct_profile(
            provider="fireworks", model=_FIREWORKS_MODEL, request=_request(image=True)
        )
    assert error.value.code == "PROXY_MODEL_CAPABILITY_UNSUPPORTED"
    assert error.value.details == {
        "profile_id": "action.executing",
        "required_capability": "image_input",
        "upstream_provider": "fireworks",
    }


def test_text_only_summary_does_not_require_its_image_preference() -> None:
    profile = resolve_direct_profile(
        provider="fireworks",
        model=_FIREWORKS_MODEL,
        request=_request(purpose="activity_summary"),
    )
    assert profile.model == _FIREWORKS_MODEL


@pytest.mark.parametrize("state", ["inline", "reference"])
def test_continuation_checks_only_images_that_will_be_sent_inline(state: str) -> None:
    request = _request().model_dump(mode="json")
    source = {**_DESCRIPTOR, "application_ref": "tool_attachment:attachment-1"}
    projection = {"state": state, "source": source}
    if state == "inline":
        projection["materialized"] = {
            "mime_type": "image/png",
            "byte_size": 10,
            "sha256": "a" * 64,
            "width_px": 1,
            "height_px": 1,
        }
    request["tool_use"] = {
        "tools": [
            {
                "name": "inspect",
                "description": "Inspect",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "max_parallel_tool_calls": 1,
        "continuation_mode": "stateless",
        "continuation": {
            "provider": "openai",
            "history_items": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_image", "detail": "high"}
                        if state == "inline"
                        else {"type": "input_text", "text": "Retained image reference"}
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "inspect",
                    "arguments": "{}",
                },
            ],
            "media_slots": [
                {
                    "item_index": 0,
                    "content_index": 0,
                    "data_field": "image_url",
                    "projection": projection,
                }
            ],
        },
        "tool_result": {"call_id": "call-1", "name": "inspect", "output": "done"},
    }
    parsed = LlmRequest.model_validate(request)
    if state == "inline":
        with pytest.raises(ProviderError) as error:
            resolve_direct_profile(
                provider="fireworks", model=_FIREWORKS_MODEL, request=parsed
            )
        assert error.value.code == "PROXY_MODEL_CAPABILITY_UNSUPPORTED"
    else:
        profile = resolve_direct_profile(
            provider="fireworks", model=_FIREWORKS_MODEL, request=parsed
        )
        assert profile.model == _FIREWORKS_MODEL


def test_known_output_limit_caps_the_purpose_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ("openai", "gpt-6-luna")
    monkeypatch.setitem(
        MODEL_CATALOG, key, replace(MODEL_CATALOG[key], max_output_tokens=4096)
    )
    profile = resolve_direct_profile(
        provider="openai", model=key[1], request=_request()
    )
    assert profile.max_output_tokens == 4096


def test_unknown_purpose_is_rejected_before_model_resolution() -> None:
    with pytest.raises(ProviderError) as error:
        resolve_direct_profile(
            provider="openai",
            model="gpt-6-luna",
            request=_request(purpose="not-a-purpose"),
        )
    assert error.value.code == "PROXY_INVALID_INPUT"
