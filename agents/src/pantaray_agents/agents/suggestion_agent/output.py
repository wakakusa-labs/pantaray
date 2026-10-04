"""Validate and normalize the Suggestion decision run's submitted output."""

import json

from pantaray_agents.schema.agent.suggestion import (
    SuggestionDecidedContent,
    SuggestionExtraction,
    SuggestionStructuredOutput,
    SuggestionTargetContext,
)


def normalize_target_context(
    target_context: SuggestionTargetContext | None,
) -> SuggestionTargetContext | None:
    if target_context is None:
        return None
    organization_name = target_context.organization_name
    project_name = target_context.project_name
    return SuggestionTargetContext(
        organization_name=organization_name.strip() or None
        if isinstance(organization_name, str)
        else None,
        project_name=project_name.strip() or None
        if isinstance(project_name, str)
        else None,
    )


def parse_suggestion_output(
    *,
    raw_text: str,
    parsed_output: SuggestionStructuredOutput | None,
) -> SuggestionExtraction:
    """SuggestionAgent 用の JSON-only LLM 出力を検証・正規化する。"""
    parsed = parsed_output
    if parsed is None:
        raw = (raw_text or "").strip()
        if not raw:
            raise ValueError("Empty structured suggestion response")
        loaded = json.loads(raw)
        parsed = SuggestionStructuredOutput.model_validate(loaded)

    key_point = parsed.key_point.strip()
    details = parsed.details.strip() if parsed.details else None
    deliverable = parsed.deliverable.strip() if parsed.deliverable else None
    suggestion_summary = (
        parsed.suggestion_summary.strip()
        if isinstance(parsed.suggestion_summary, str)
        else None
    )
    target_context = normalize_target_context(parsed.target_context)

    if parsed.has_suggestion:
        if not key_point:
            raise ValueError("key_point must be non-empty when has_suggestion=true")
        if parsed.interaction_contract is None:
            raise ValueError(
                "interaction_contract is required when has_suggestion=true"
            )
        if (parsed.interaction_contract == "action_offer") != bool(deliverable):
            raise ValueError(
                "deliverable must be given exactly when interaction_contract "
                "is action_offer"
            )
        if parsed.agent_session is None:
            raise ValueError("agent_session is required when has_suggestion=true")
        if not suggestion_summary:
            raise ValueError(
                "suggestion_summary must be non-empty when has_suggestion=true"
            )
        if target_context is None:
            raise ValueError(
                "target_context must be an object when has_suggestion=true"
            )
        decided: SuggestionDecidedContent = {
            "interaction_contract": parsed.interaction_contract,
            "key_point": key_point,
            "deliverable": deliverable,
            "agent_session": parsed.agent_session,
        }
        return {
            "thinking": None,
            "answer": "",
            "decided": decided,
            "suggestion_summary": suggestion_summary,
            "target_context": target_context,
            "prompt_text": "",
            "response_text": raw_text,
            "has_suggestion": True,
            "interaction_contract": parsed.interaction_contract,
        }

    if key_point or details or deliverable:
        raise ValueError(
            "key_point, details and deliverable must be empty when has_suggestion=false"
        )
    if parsed.interaction_contract is not None:
        raise ValueError("interaction_contract must be null when has_suggestion=false")
    if suggestion_summary:
        raise ValueError("suggestion_summary must be empty when has_suggestion=false")
    if target_context is not None:
        raise ValueError("target_context must be null when has_suggestion=false")
    return {
        "thinking": None,
        "answer": "",
        "decided": None,
        "suggestion_summary": None,
        "target_context": None,
        "prompt_text": "",
        "response_text": raw_text,
        "has_suggestion": False,
        "interaction_contract": None,
    }
