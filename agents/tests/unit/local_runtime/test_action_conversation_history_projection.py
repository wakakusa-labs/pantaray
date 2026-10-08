from dataclasses import replace
from unittest.mock import Mock

import pytest

from pantaray_agents.local_runtime.action_conversation import (
    history_candidates,
    history_projection,
    history_queries,
    repository,
    terminal_projection,
)
from pantaray_agents.local_runtime.action_conversation.history_projection import (
    project_conversation_history_page,
    required_history_run_selections,
)
from pantaray_agents.local_runtime.runtime import (
    action_logical_run_authority as authority,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    ChatHandoffInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
    serialize_action_user_message,
)
from pantaray_agents.schema.conversation_history import ConversationHistoryPage

_AT = "2026-08-30T00:00:00.000Z"
_DONE = "completed"
_MESSAGE_JSON = '{"message_id":"m","content":"Public title"}'
_USER = history_queries.ActionHistoryUserRow(
    "u", 1, 1, "m", _MESSAGE_JSON, "Public title", "r", None, None, "user_request"
)
_TERMINAL = authority.ActionLogicalRunAuthority(
    "a", "r", 1, _AT, _DONE, "j", _DONE, _AT, "e", "{}"
)
_ACTIVE = replace(
    _TERMINAL,
    process_status="running",
    job_status="running",
    completed_at=None,
    terminal_event_id=None,
    terminal_event_payload_json=None,
)


_ACTION = history_candidates.ActionHistoryCandidate(
    "a",
    "linked",
    "processing",
    "user-1",
    "action_offer",
    "accepted",
    "m",
    _USER,
    "r",
    "running",
    None,
    None,
    None,
    _AT,
    None,
    None,
)
_SUGGESTION = history_candidates.SuggestionHistoryCandidate(
    "suggestion-1", "Approve this", "action_offer", None, "approval_pending", _AT
)


def _project(
    candidates: tuple[history_candidates.ConversationHistoryCandidate, ...],
    authorities: tuple[authority.ActionLogicalRunAuthority, ...] = (),
    search: str = "",
) -> ConversationHistoryPage:
    return project_conversation_history_page(
        candidate_page=history_candidates.ConversationHistoryCandidatePage(
            candidates, "next-page"
        ),
        user_id="user-1",
        normalized_search=search,
        authorities=authorities,
    )


def test_projects_only_strict_public_fields_and_canonical_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = Mock(
        return_value=terminal_projection.ActionRunTerminalProjection(
            "success", _AT, "Needle", None
        )
    )
    monkeypatch.setattr(history_projection, "project_action_run_terminal", terminal)
    old_terminal = replace(_TERMINAL, root_process_id="old-r")
    candidates: tuple[history_candidates.ConversationHistoryCandidate, ...] = (
        replace(
            _ACTION,
            action_status="success",
            status_hint="idle",
            latest_completion_event_id="e",
            matched_terminal_root_run_id="old-r",
        ),
        replace(_SUGGESTION, answer="Needle approval"),
    )
    page = _project(candidates, (_TERMINAL, old_terminal), "needle")
    assert [(item.kind, item.title, item.status) for item in page.items] == [
        ("conversation", "Public title", "idle"),
        ("suggestion", "Needle approval", "approval_pending"),
    ]
    assert page.items[0].model_dump()["latest_completion_event_id"] == "e"
    assert "final_output" not in page.model_dump_json()
    assert required_history_run_selections(candidates) == frozenset(
        authority.ActionLogicalRunSelection("a", run_id) for run_id in ("r", "old-r")
    )
    active = _project((replace(_ACTION, matched_user=_USER),), (_ACTIVE,), "public")
    assert active.items[0].status == "running"


def test_malformed_candidate_or_authority_fails_closed() -> None:
    cases = (
        ((_ACTION,), (), ""),
        ((_ACTION,), (_ACTIVE, _ACTIVE), ""),
        ((replace(_ACTION, action_status="success", latest_run_id=None),), (), ""),
        ((replace(_ACTION, status_hint="idle"),), (_ACTIVE,), ""),
        ((replace(_ACTION, action_status="success"),), (_ACTIVE,), ""),
        (
            (replace(_ACTION, action_status="queued", status_hint="approval_pending"),),
            (replace(_ACTIVE, process_status="paused", job_status="paused"),),
            "",
        ),
        ((replace(_ACTION, suggestion_user_id="other"),), (_ACTIVE,), ""),
        ((replace(_SUGGESTION, user_reaction="accepted"),), (), ""),
        ((replace(_SUGGESTION, user_reaction="unknown"),), (), ""),
        ((_ACTION,), (_ACTIVE,), "needle"),
    )
    for candidates, authorities, search in cases:
        with pytest.raises(repository.ActionConversationIntegrityError):
            _project(candidates, authorities, search)


@pytest.mark.parametrize("supplement", [None, "Include the risks"])
def test_approved_proposal_remains_the_history_title_and_search_evidence(
    supplement: str | None,
) -> None:
    message = ActionUserMessageInput(
        message_id="m",
        content="Review CAFÉ",
        supplement=supplement,
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="linked",
            approved_at=_AT,
            summary="Private provenance",
        ),
    )
    row = replace(
        _USER,
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
    )
    candidate = replace(_ACTION, initial_user=row)
    expected_title = "Review CAFÉ" + (
        "\n\nAdditional user conditions:\nInclude the risks" if supplement else ""
    )
    page = _project((candidate,), (_ACTIVE,))
    assert page.items[0].title == expected_title
    for search in ("café", "risks") if supplement else ("café",):
        page = _project((replace(candidate, matched_user=row),), (_ACTIVE,), search)
        assert page.items[0].title == expected_title
    with pytest.raises(repository.ActionConversationIntegrityError):
        _project(
            (replace(candidate, matched_user=row),), (_ACTIVE,), "private provenance"
        )


def test_a_task_the_chat_started_in_its_own_words_takes_them_as_its_title() -> None:
    message = ActionUserMessageInput(
        message_id="m",
        content="List the customers from that history",
        chat_handoff=ChatHandoffInput(relayed_item_ids=()),
    )
    row = replace(
        _USER,
        user_message_json=serialize_action_user_message(message),
        user_request_text=render_action_user_request_text(message),
    )
    page = _project((replace(_ACTION, initial_user=row),), (_ACTIVE,))
    assert page.items[0].title == "List the customers from that history"
