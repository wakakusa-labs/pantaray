import type {
  ActionFileAttachment,
  ActionMessageRequest,
  ActionProjectRef,
} from '../actions/actionContracts';

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export type UiLanguage = 'en' | 'ja';

export const ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS = 8_000;

export function normalizeActionSupplement(value: string): string | null {
  return value.trim() || null;
}

export function isActionSupplementWithinLimit(value: string): boolean {
  return [...value].length <= ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS;
}

export type AcceptActionRequest = {
  suggestionId: string;
  commandId: string | null;
  supplement: string | null;
  // Spans point into the trimmed supplement.
  supplementProjectRefs: ActionProjectRef[];
  approvalMode: 'prompt_each_time' | 'always_allow';
  images: ActionMessageRequest['message']['images'];
  files?: ActionFileAttachment[];
};

export type ActionErrorStage =
  | 'preflight_rejected'
  | 'start_failed'
  | 'running_failed'
  | 'persist_final_state_failed';

export type ProcessKind = 'suggestion' | 'action';
export type SuggestionInteractionContract = 'action_offer' | 'message_only';
export type ActionTerminalStatus = 'success' | 'error' | 'canceled' | 'timeout';
export type ActionDisplayStatus = 'idle' | 'processing' | ActionTerminalStatus;

export type OrchestrationCommandPhase =
  | 'requesting'
  | 'accepted_pending_start'
  | 'processing'
  | 'terminal';

export type OrchestrationEventMeta = {
  process_id?: string | null;
  suggestion_id?: string | null;
  action_id?: string | null;
  command_id?: string | null;
  kind?: ProcessKind | null;
  stage?: string | null;
  error_code?: string | null;
};

export type SuggestionLiveMeta = OrchestrationEventMeta & {
  kind: 'suggestion';
  process_id: string;
  suggestion_id: string | null;
};

export type SuggestionIdentityMeta = {
  suggestion_id: string;
};

export type ActionLiveMeta = OrchestrationEventMeta & {
  kind: 'action';
  suggestion_id: string | null;
  process_id: string;
  action_id: string;
};

export type ActionProcessMeta = ActionLiveMeta & {
  command_id: string;
};

export type ActionErrorMeta = {
  kind: 'action';
  suggestion_id: string;
  command_id: string;
  stage: ActionErrorStage;
  error_code: string;
  process_id?: string | null;
  action_id?: string | null;
  failure_kind?: string | null;
};

type OrchestrationEventEnvelope<E extends string, D> = {
  event: E;
  data: D;
  event_id?: string | null;
  sequence?: number | null;
};

type OrchestrationEventWithMeta<E extends string, D, M> = OrchestrationEventEnvelope<E, D> & {
  meta: M;
};

export type ExecuteActionClientEvent = {
  event: 'execute_action';
  data: {
    suggestion_id: string;
    command_id: string;
    language: UiLanguage;
    supplement: string | null;
    supplement_project_refs: AcceptActionRequest['supplementProjectRefs'];
    approval_mode: AcceptActionRequest['approvalMode'];
    images: AcceptActionRequest['images'];
    files?: AcceptActionRequest['files'];
  };
};

export type DismissSuggestionClientEvent = {
  event: 'dismiss_suggestion';
  data: { suggestion_id: string; reason?: string };
};

export type RejectSuggestionClientEvent = {
  event: 'reject_suggestion';
  data: { suggestion_id: string; reason?: string };
};

export type StopProcessClientEvent = {
  event: 'stop_process';
  data: { process_id: string };
};

export type ResumeProcessClientEvent = {
  event: 'resume_process';
  data: {
    kind?: ProcessKind;
    process_id?: string;
    processId?: string;
    suggestion_id?: string;
    suggestionId?: string;
    action_id?: string;
    actionId?: string;
    command_id?: string;
    commandId?: string;
    fromStart?: boolean;
  };
};

export type ResumeSessionClientEvent = {
  event: 'resume_session';
  data: {
    session_id: string;
    last_cursor: string | null;
    process_id: string;
    last_chunk_index: number;
    kind: ProcessKind;
    suggestion_id?: string;
    action_id?: string;
    command_id?: string;
  };
};

export type AckEventClientEvent = {
  event: 'ack_event';
  data: { session_id: string; process_id: string; event_id: string };
};

export type RendererOrchestrationClientEvent =
  | DismissSuggestionClientEvent
  | RejectSuggestionClientEvent
  | StopProcessClientEvent
  | ResumeProcessClientEvent
  | ResumeSessionClientEvent
  | AckEventClientEvent;

export type OrchestrationClientEvent = RendererOrchestrationClientEvent | ExecuteActionClientEvent;

export type SuggestionChunkEvent = OrchestrationEventWithMeta<
  'suggestion_chunk',
  { content: string },
  SuggestionLiveMeta
>;

export type SuggestionReactionCommittedEvent = OrchestrationEventWithMeta<
  'suggestion_reaction_committed',
  {
    suggestion_id: string;
    reaction: 'accepted' | 'rejected';
    committed_at: string;
  },
  SuggestionIdentityMeta
>;

export type ActionRequestedEvent = OrchestrationEventWithMeta<
  'action_requested',
  {
    suggestion_id: string;
    command_id: string;
    accepted_at: string;
    committed_at: string;
  },
  SuggestionIdentityMeta & { kind: 'action'; command_id: string }
>;

export type ProcessStartedSuggestionEvent = OrchestrationEventWithMeta<
  'process_started',
  {
    kind: 'suggestion';
    process_id: string;
    suggestion_id?: string | null;
  },
  SuggestionLiveMeta
>;

export type ProcessStartedActionEvent = OrchestrationEventWithMeta<
  'process_started',
  {
    kind: 'action';
    process_id: string;
    suggestion_id: string | null;
    action_id: string;
    command_id: string;
    accepted_at: string;
    started_at: string;
  },
  ActionProcessMeta
>;

export type ActionStepEvent = OrchestrationEventWithMeta<
  'action_step',
  | {
      action_id: string;
      process_id: string;
      step_kind: 'tool';
      step_id: string;
      step_number: number;
      tool_id: string;
      label: string;
      subject?: string | null;
      status: 'processing' | 'success' | 'error' | 'timeout';
      started_at: string;
      completed_at: string | null;
    }
  | {
      action_id: string;
      process_id: string;
      step_kind: 'assistant';
      step_id: string;
      step_number: number;
      status: 'success';
    },
  ActionLiveMeta & { logical_run_id: string }
>;

export type CompletionChunkEvent = OrchestrationEventWithMeta<
  'completion_chunk',
  { content: string },
  ActionLiveMeta
>;

export type ProcessCompletedSuggestionEvent = OrchestrationEventWithMeta<
  'process_completed',
  {
    kind: 'suggestion';
    process_id: string;
    status: ActionTerminalStatus;
    has_suggestion?: boolean | null;
    suggestion_id?: string | null;
    interaction_contract?: SuggestionInteractionContract | null;
  },
  SuggestionLiveMeta
>;

export type ProcessCompletedActionEvent = OrchestrationEventWithMeta<
  'process_completed',
  {
    kind: 'action';
    process_id: string;
    status: ActionTerminalStatus;
    suggestion_id: string | null;
    action_id: string;
    command_id: string;
    has_suggestion?: boolean | null;
  },
  ActionProcessMeta
>;

export type ProcessPausedActionEvent = OrchestrationEventWithMeta<
  'process_paused',
  {
    kind: 'action';
    process_id: string;
    status: 'processing';
    reason: 'approval_pending';
    suggestion_id: string | null;
    action_id: string;
    command_id: string;
    completed_at: string;
    approval_blockers: ApprovalBlocker[];
  },
  ActionProcessMeta
>;

export type ApprovalBlocker = {
  // The physical process held by this blocker: the Action root or one of its subagents.
  process_id: string;
  action_id: string;
  approval_session_id: string;
  tool_request_id: string;
  tool_id: string;
  intent_class: string;
  command_summary: JsonObject;
};

export type SessionResumedEvent = OrchestrationEventEnvelope<
  'session_resumed',
  { resumed_from_chunk: number; missing_chunks: number[] }
>;

export type SessionExpiredEvent = OrchestrationEventEnvelope<
  'session_expired',
  { reason: string; max_session_age_seconds?: number }
>;

export type OrchestrationErrorEvent = OrchestrationEventEnvelope<
  'error',
  {
    error_type: string;
    error_code: string;
    error_message?: string | null;
    error_details?: JsonObject | null;
    severity: 'info' | 'warning' | 'error' | 'critical';
    metadata?: JsonObject | null;
  }
> & {
  meta?: OrchestrationEventMeta | ActionErrorMeta;
};

/**
 * A control message for the desktop client, never forwarded to a renderer.
 *
 * The Action runtime cannot reach the screen; it asks here, and the answer goes back
 * over the local backend. `capture_request_id` is single-use, so a replayed event
 * cannot make a second capture.
 */
export type ScreenCaptureRequestedEvent = OrchestrationEventEnvelope<
  'screen_capture_requested',
  {
    kind: 'action';
    process_id: string;
    action_id: string;
    tool_request_id: string;
    capture_request_id: string;
    app_name: string;
  }
>;

export type OrchestrationServerEvent =
  | SuggestionChunkEvent
  | SuggestionReactionCommittedEvent
  | ActionRequestedEvent
  | ProcessStartedSuggestionEvent
  | ProcessStartedActionEvent
  | ActionStepEvent
  | CompletionChunkEvent
  | ProcessPausedActionEvent
  | ScreenCaptureRequestedEvent
  | ProcessCompletedSuggestionEvent
  | ProcessCompletedActionEvent
  | SessionResumedEvent
  | SessionExpiredEvent
  | OrchestrationErrorEvent;

export function hasEventMeta(
  event: OrchestrationServerEvent
): event is OrchestrationServerEvent & { meta: OrchestrationEventMeta | ActionErrorMeta } {
  return 'meta' in event && Boolean(event.meta);
}

export function getEventMeta(
  event: OrchestrationServerEvent
): OrchestrationEventMeta | ActionErrorMeta | null {
  return hasEventMeta(event) ? event.meta : null;
}

export function isSuggestionReactionCommittedEvent(
  event: OrchestrationServerEvent
): event is SuggestionReactionCommittedEvent {
  return event.event === 'suggestion_reaction_committed';
}

export function isActionRequestedEvent(
  event: OrchestrationServerEvent
): event is ActionRequestedEvent {
  return event.event === 'action_requested';
}

export function isProcessStartedActionEvent(
  event: OrchestrationServerEvent
): event is ProcessStartedActionEvent {
  return event.event === 'process_started' && event.data.kind === 'action';
}

export function isProcessStartedSuggestionEvent(
  event: OrchestrationServerEvent
): event is ProcessStartedSuggestionEvent {
  return event.event === 'process_started' && event.data.kind === 'suggestion';
}

export function isProcessCompletedActionEvent(
  event: OrchestrationServerEvent
): event is ProcessCompletedActionEvent {
  return event.event === 'process_completed' && event.data.kind === 'action';
}

export function isProcessPausedActionEvent(
  event: OrchestrationServerEvent
): event is ProcessPausedActionEvent {
  return event.event === 'process_paused' && event.data.kind === 'action';
}

export function isScreenCaptureRequestedEvent(
  event: OrchestrationServerEvent
): event is ScreenCaptureRequestedEvent {
  return event.event === 'screen_capture_requested';
}

export function isProcessCompletedSuggestionEvent(
  event: OrchestrationServerEvent
): event is ProcessCompletedSuggestionEvent {
  return event.event === 'process_completed' && event.data.kind === 'suggestion';
}

export function isCompletionChunkEvent(
  event: OrchestrationServerEvent
): event is CompletionChunkEvent {
  return event.event === 'completion_chunk';
}

export function isSuggestionChunkEvent(
  event: OrchestrationServerEvent
): event is SuggestionChunkEvent {
  return event.event === 'suggestion_chunk';
}

export function isErrorEvent(event: OrchestrationServerEvent): event is OrchestrationErrorEvent {
  return event.event === 'error';
}

export type OrchestrationStatus = {
  status:
    | 'connected'
    | 'closed'
    | 'error'
    | 'session_started'
    | 'unexpected_response'
    | 'resume_requested'
    | 'session_resumed';
  code?: number;
  error?: string;
  session_id?: string | null;
  previous_session_id?: string | null;
  process_id?: string | null;
  suggestion_id?: string | null;
  action_id?: string | null;
  resumed_from?: number;
  missing?: number;
};
