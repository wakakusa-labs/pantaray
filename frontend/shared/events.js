// WebSocket wire event names の共有定義
// CommonJS でエクスポートし、Electronメイン/レンダラー双方から参照可能

const InboundEvent = Object.freeze({
  RESUME_SESSION: 'resume_session',
  ACK_EVENT: 'ack_event',
});

const OutboundEvent = Object.freeze({
  SESSION_STARTED: 'session_started',
  SUGGESTION_CHUNK: 'suggestion_chunk',
  SUGGESTION_REACTION_COMMITTED: 'suggestion_reaction_committed',
  ACTION_REQUESTED: 'action_requested',
  PROCESS_STARTED: 'process_started',
  COMPLETION_CHUNK: 'completion_chunk',
  ACTION_STEP: 'action_step',
  PROCESS_PAUSED: 'process_paused',
  SCREEN_CAPTURE_REQUESTED: 'screen_capture_requested',
  HTML_RENDER_REQUESTED: 'html_render_requested',
  PROCESS_COMPLETED: 'process_completed',
  SESSION_RESUMED: 'session_resumed',
  SESSION_EXPIRED: 'session_expired',
  ERROR: 'error',
});

module.exports = { InboundEvent, OutboundEvent };
