from __future__ import annotations

from typing import Literal, cast

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.action_logical_run_authority import (
    ActionLogicalRunAuthority,
    ActionLogicalRunSelection,
)
from pantaray_agents.schema.action_conversation import (
    ActionConversationSummary,
    ActionStatus,
    UserEntry,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_visible_text,
)
from pantaray_agents.schema.conversation_history import (
    ConversationHistoryItem,
    ConversationHistoryPage,
    ConversationHistoryStatus,
    SuggestionHistoryItem,
)

from .entry_projection import project_action_user_entry
from .history_candidates import (
    ActionHistoryCandidate,
    ConversationHistoryCandidate,
    ConversationHistoryCandidatePage,
    SuggestionHistoryCandidate,
)
from .repository import ActionConversationIntegrityError
from .terminal_projection import project_action_run_terminal

_ACTIVE_HISTORY_STATUS: dict[tuple[str, str], ConversationHistoryStatus] = {
    ("enqueued", "queued"): "running",
    ("running", "running"): "running",
    ("paused", "paused"): "approval_pending",
}
type _AuthorityMap = dict[ActionLogicalRunSelection, ActionLogicalRunAuthority]


def _require(condition: bool) -> None:
    if not condition:
        raise ActionConversationIntegrityError("Invalid conversation history evidence")


def required_history_run_selections(
    candidates: tuple[ConversationHistoryCandidate, ...],
) -> frozenset[ActionLogicalRunSelection]:
    return frozenset(
        ActionLogicalRunSelection(candidate.action_id, run_id)
        for candidate in candidates
        if isinstance(candidate, ActionHistoryCandidate)
        for run_id in (candidate.latest_run_id, candidate.matched_terminal_root_run_id)
        if run_id is not None
    )


def project_conversation_history_page(
    *,
    candidate_page: ConversationHistoryCandidatePage,
    user_id: str,
    normalized_search: str,
    authorities: tuple[ActionLogicalRunAuthority, ...],
) -> ConversationHistoryPage:
    selections = required_history_run_selections(candidate_page.candidates)
    authority_by_selection = {
        ActionLogicalRunSelection(
            authority.action_id, authority.root_process_id
        ): authority
        for authority in authorities
    }
    _require(
        frozenset(authority_by_selection) == selections
        and len(authority_by_selection) == len(authorities)
    )

    try:
        items = tuple(
            _project_action(
                candidate,
                user_id=user_id,
                normalized_search=normalized_search,
                authorities=authority_by_selection,
            )
            if isinstance(candidate, ActionHistoryCandidate)
            else _project_suggestion(candidate, normalized_search=normalized_search)
            for candidate in candidate_page.candidates
        )
        return ConversationHistoryPage(
            items=items, next_cursor=candidate_page.next_cursor
        )
    except ValidationError as exc:
        raise ActionConversationIntegrityError(
            "Conversation history candidates cannot form a public page"
        ) from exc


def _project_suggestion(
    candidate: SuggestionHistoryCandidate,
    *,
    normalized_search: str,
) -> SuggestionHistoryItem:
    reaction = candidate.user_reaction
    _require(
        (
            candidate.interaction_contract == "action_offer"
            and reaction in {None, "rejected"}
        )
        or (candidate.interaction_contract == "message_only" and reaction is None)
    )
    expected_status: Literal["approval_pending", "idle"] = (
        "approval_pending"
        if candidate.interaction_contract == "action_offer" and reaction is None
        else "idle"
    )
    _require(candidate.status_hint == expected_status)
    title = candidate.answer
    _require(isinstance(title, str))
    assert isinstance(title, str)
    _require(not normalized_search or normalized_search in title.casefold())
    return SuggestionHistoryItem(
        kind="suggestion",
        suggestion_id=candidate.suggestion_id,
        title=title,
        updated_at=candidate.updated_at,
        status=expected_status,
    )


def _project_action(
    candidate: ActionHistoryCandidate,
    *,
    user_id: str,
    normalized_search: str,
    authorities: _AuthorityMap,
) -> ConversationHistoryItem:
    expected_suggestion = (
        (None, None, None)
        if candidate.suggestion_id is None
        else (user_id, "action_offer", "accepted")
    )
    _require(
        (
            candidate.suggestion_user_id,
            candidate.suggestion_interaction_contract,
            candidate.suggestion_user_reaction,
        )
        == expected_suggestion
    )
    initial_user = candidate.initial_user
    _require(
        initial_user is not None
        and initial_user.user_message_id == candidate.initial_message_id
    )
    assert initial_user is not None
    title_entry = project_action_user_entry(initial_user)
    _require(title_entry.status == "adopted")
    _require(candidate.latest_run_id is not None)
    latest_run_id = cast("str", candidate.latest_run_id)
    action = ActionConversationSummary(
        action_id=candidate.action_id,
        suggestion_id=candidate.suggestion_id,
        approved_suggestion=title_entry.approved_suggestion,
        status=cast("ActionStatus", candidate.action_status),
        latest_run_id=latest_run_id,
        # The history list opens a conversation; 「再開」 is offered by the
        # conversation page, which is where resumability is read.
        resumable=False,
    )
    latest_authority = authorities[
        ActionLogicalRunSelection(candidate.action_id, latest_run_id)
    ]
    history_status = _ACTIVE_HISTORY_STATUS.get(
        (latest_authority.process_status, latest_authority.job_status),
        "idle",
    )
    completion_event_id = None
    if history_status == "running":
        _require(action.status in {"queued", "processing"})
    elif history_status == "approval_pending":
        _require(action.status == "processing")
    else:
        terminal = project_action_run_terminal(
            user_id=user_id, action=action, authority=latest_authority
        )
        _require(terminal.status == action.status)
        completion_event_id = latest_authority.terminal_event_id
    _require(
        candidate.status_hint == history_status
        and candidate.latest_completion_event_id == completion_event_id
    )
    user_proof = candidate.matched_user
    terminal_run_id = candidate.matched_terminal_root_run_id
    assistant_text = candidate.matched_assistant_text
    if not normalized_search:
        _require(
            user_proof is None and terminal_run_id is None and assistant_text is None
        )
    else:
        _require(
            sum(
                proof is not None
                for proof in (user_proof, terminal_run_id, assistant_text)
            )
            == 1
        )
        if user_proof is not None:
            _require(
                normalized_search
                in _user_search_text(project_action_user_entry(user_proof)).casefold()
            )
        elif assistant_text is not None:
            _require(normalized_search in assistant_text.casefold())
        else:
            assert terminal_run_id is not None
            authority = authorities[
                ActionLogicalRunSelection(candidate.action_id, terminal_run_id)
            ]
            terminal = project_action_run_terminal(
                user_id=user_id,
                action=action,
                authority=authority,
            )
            _require(
                terminal.final_output is not None
                and normalized_search in terminal.final_output.casefold()
            )
    return ConversationHistoryItem(
        kind="conversation",
        action_id=candidate.action_id,
        # A conversation that opens with a replied-to Suggestion keeps the
        # title that Suggestion had in the list before the reply.
        title=(
            _user_history_text(title_entry)
            if candidate.opening_suggestion_text is None
            else candidate.opening_suggestion_text
        ),
        updated_at=candidate.updated_at,
        status=history_status,
        latest_completion_event_id=completion_event_id,
    )


def _user_search_text(entry: UserEntry) -> str:
    """What a search finds a message by: what the History shows of it, and the
    chat's note shown beside the user's words."""

    text = _user_history_text(entry)
    beside = entry.content is not None or entry.approved_suggestion is not None
    return f"{text}\n{entry.chat_note}" if beside and entry.chat_note else text


def _user_history_text(entry: UserEntry) -> str:
    if entry.approved_suggestion is not None:
        return render_action_user_visible_text(
            content=entry.approved_suggestion.content, supplement=entry.content
        )
    if entry.content is None:
        # The chat handed the task over in its own words.
        assert entry.chat_note is not None  # USER validation requires one of them
        return entry.chat_note
    return entry.content


__all__ = ["project_conversation_history_page", "required_history_run_selections"]
