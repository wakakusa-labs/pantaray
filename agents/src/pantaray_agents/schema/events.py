"""Inbound/Outbound WebSocket event names."""

from enum import StrEnum


class InboundEvent(StrEnum):
    EXECUTE_ACTION = "execute_action"
    # NOTE: "reject_suggestion" は用語変更により非推奨。後方互換のため残す。
    REJECT_SUGGESTION = "reject_suggestion"
    # よりソフトな表現として "dismiss" を採用（UI文言も合わせる）。
    DISMISS_SUGGESTION = "dismiss_suggestion"
    RESUME_SESSION = "resume_session"
    STOP_PROCESS = "stop_process"
    ACK_EVENT = "ack_event"


class OutboundEvent(StrEnum):
    SUGGESTION_CHUNK = "suggestion_chunk"
    SUGGESTION_REACTION_COMMITTED = "suggestion_reaction_committed"
    ACTION_REQUESTED = "action_requested"
    PROCESS_STARTED = "process_started"
    COMPLETION_CHUNK = "completion_chunk"
    ACTION_STEP = "action_step"
    PROCESS_PAUSED = "process_paused"
    SCREEN_CAPTURE_REQUESTED = "screen_capture_requested"
    PROCESS_COMPLETED = "process_completed"
    SESSION_RESUMED = "session_resumed"
    SESSION_EXPIRED = "session_expired"
    SESSION_STARTED = "session_started"
    CHAT_ITEM_APPENDED = "chat_item_appended"
    CHAT_TURN_STATE = "chat_turn_state"
    ERROR = "error"
