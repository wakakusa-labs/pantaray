import type { AgentOverlayState, OverlayServerEvent } from './overlayTypes';
import { isActionErrorMeta } from './overlayTypes';

export function normalizeId(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const s = String(value);
  return s.length > 0 ? s : null;
}

function normalizeSequence(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
export function applySuggestionId(
  state: AgentOverlayState,
  suggestionId: string | null
): AgentOverlayState {
  if (!suggestionId) return state;
  if (state.suggestionId === suggestionId) return state;
  return {
    ...state,
    suggestionId,
    currentActionId: null,
    currentCommandId: null,
    interactionContract: null,
    dismissReason: null,
    historyExpandOverride: null,
    knownProcessIds: new Set<string>(),
    knownActionIds: new Set<string>(),
    decisionLocked: false,
    requestState: 'idle',
  };
}

type ExtractedIds = {
  suggestionId: string | null;
  processId: string | null;
  actionId: string | null;
  kind: 'suggestion' | 'action' | null;
};
type ProcessStartedEvent = Extract<OverlayServerEvent, { event: 'process_started' }>;
type ProcessCompletedEvent = Extract<OverlayServerEvent, { event: 'process_completed' }>;
type ProcessPausedEvent = Extract<OverlayServerEvent, { event: 'process_paused' }>;
type ActionProcessStartedEvent = Extract<ProcessStartedEvent, { data: { kind: 'action' } }>;
type ActionProcessCompletedEvent = Extract<ProcessCompletedEvent, { data: { kind: 'action' } }>;
type ActionProcessPausedEvent = Extract<ProcessPausedEvent, { data: { kind: 'action' } }>;
function normalizeKind(value: unknown): ExtractedIds['kind'] {
  return value === 'suggestion' || value === 'action' ? value : null;
}
function getLifecycleEventKind(
  ev: ProcessStartedEvent | ProcessCompletedEvent | ProcessPausedEvent
): ExtractedIds['kind'] {
  return normalizeKind(ev.data.kind ?? ev.meta.kind);
}
function isActionProcessStartedEvent(ev: ProcessStartedEvent): ev is ActionProcessStartedEvent {
  return getLifecycleEventKind(ev) === 'action';
}
function isActionProcessCompletedEvent(
  ev: ProcessCompletedEvent
): ev is ActionProcessCompletedEvent {
  return getLifecycleEventKind(ev) === 'action';
}
function isActionProcessPausedEvent(ev: ProcessPausedEvent): ev is ActionProcessPausedEvent {
  return getLifecycleEventKind(ev) === 'action';
}
function extractIds(ev: OverlayServerEvent): ExtractedIds {
  switch (ev.event) {
    case 'suggestion_chunk':
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id),
        processId: normalizeId(ev.meta.process_id),
        actionId: null,
        kind: ev.meta.kind,
      };
    case 'suggestion_reaction_committed':
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
        processId: null,
        actionId: null,
        kind: normalizeKind(ev.meta.suggestion_id ? 'suggestion' : null),
      };
    case 'action_requested':
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
        processId: null,
        actionId: null,
        kind: normalizeKind(ev.meta.kind ?? 'action'),
      };
    case 'process_started':
      if (isActionProcessStartedEvent(ev)) {
        return {
          suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
          processId: normalizeId(ev.meta.process_id ?? ev.data.process_id),
          actionId: normalizeId(ev.meta.action_id ?? ev.data.action_id),
          kind: normalizeKind(ev.meta.kind ?? ev.data.kind),
        };
      }
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
        processId: normalizeId(ev.meta.process_id ?? ev.data.process_id),
        actionId: null,
        kind: normalizeKind(ev.meta.kind ?? ev.data.kind),
      };
    case 'process_completed':
      if (isActionProcessCompletedEvent(ev)) {
        return {
          suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
          processId: normalizeId(ev.meta.process_id ?? ev.data.process_id),
          actionId: normalizeId(ev.meta.action_id ?? ev.data.action_id),
          kind: normalizeKind(ev.meta.kind ?? ev.data.kind),
        };
      }
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
        processId: normalizeId(ev.meta.process_id ?? ev.data.process_id),
        actionId: null,
        kind: normalizeKind(ev.meta.kind ?? ev.data.kind),
      };
    case 'process_paused':
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id ?? ev.data.suggestion_id),
        processId: normalizeId(ev.meta.process_id ?? ev.data.process_id),
        actionId: normalizeId(ev.meta.action_id ?? ev.data.action_id),
        kind: normalizeKind(ev.meta.kind ?? ev.data.kind),
      };
    case 'completion_chunk':
      return {
        suggestionId: normalizeId(ev.meta.suggestion_id),
        processId: normalizeId(ev.meta.process_id),
        actionId: normalizeId(ev.meta.action_id),
        kind: ev.meta.kind,
      };
    case 'error':
      return {
        suggestionId: normalizeId(ev.meta?.suggestion_id),
        processId: normalizeId(ev.meta?.process_id),
        actionId: normalizeId(ev.meta?.action_id),
        kind: normalizeKind(ev.meta?.kind),
      };
    default:
      return {
        suggestionId: null,
        processId: null,
        actionId: null,
        kind: null,
      };
  }
}
function extractCommandId(ev: OverlayServerEvent): string | null {
  switch (ev.event) {
    case 'process_started':
      return isActionProcessStartedEvent(ev)
        ? normalizeId(ev.data.command_id ?? ev.meta.command_id)
        : null;
    case 'action_requested':
      return normalizeId(ev.data.command_id ?? ev.meta.command_id);
    case 'process_completed':
      return isActionProcessCompletedEvent(ev)
        ? normalizeId(ev.data.command_id ?? ev.meta.command_id)
        : null;
    case 'process_paused':
      return isActionProcessPausedEvent(ev)
        ? normalizeId(ev.data.command_id ?? ev.meta.command_id)
        : null;
    case 'error':
      return normalizeId(ev.meta?.command_id);
    default:
      return null;
  }
}
function belongsToOverlay(state: AgentOverlayState, ids: ExtractedIds): boolean {
  const overlaySuggestionId = state.suggestionId;
  const matchesSuggestion = Boolean(
    overlaySuggestionId && ids.suggestionId && overlaySuggestionId === ids.suggestionId
  );
  const matchesProcess = Boolean(ids.processId && state.knownProcessIds.has(ids.processId));
  const matchesAction = Boolean(ids.actionId && state.knownActionIds.has(ids.actionId));
  return Boolean(
    (overlaySuggestionId && (matchesSuggestion || matchesProcess || matchesAction)) ||
    (!overlaySuggestionId && ids.suggestionId)
  );
}
function appendChunk(prev: string, chunk: string): string {
  if (!prev) return chunk;
  return `${prev}\n${chunk}`;
}
export function reduceServerEvent(
  state: AgentOverlayState,
  ev: OverlayServerEvent
): AgentOverlayState {
  const eventSequence = normalizeSequence(ev.sequence);
  const ids = extractIds(ev);
  let next = state;
  if (!next.suggestionId && ids.suggestionId) {
    next = applySuggestionId(next, ids.suggestionId);
  }
  if (!belongsToOverlay(next, ids)) {
    return state;
  }
  if (eventSequence !== null && eventSequence <= next.lastSequence) {
    return state;
  }
  if (ids.suggestionId) {
    next = applySuggestionId(next, ids.suggestionId);
  }
  if (next.suggestionId) {
    if (ids.processId && !next.knownProcessIds.has(ids.processId)) {
      const s = new Set(next.knownProcessIds);
      s.add(ids.processId);
      next = { ...next, knownProcessIds: s };
    }
    if (ids.actionId && !next.knownActionIds.has(ids.actionId)) {
      const s = new Set(next.knownActionIds);
      s.add(ids.actionId);
      next = { ...next, knownActionIds: s };
    }
  }
  const eventCommandId = extractCommandId(ev);
  const finalizeSequence = (value: AgentOverlayState): AgentOverlayState =>
    eventSequence === null ? value : { ...value, lastSequence: eventSequence };
  if (ev.event === 'error') {
    const meta = ev.meta;
    if (!isActionErrorMeta(meta)) {
      return next;
    }
    if (next.currentCommandId && meta.command_id !== next.currentCommandId) {
      return next;
    }
    if (meta.stage === 'preflight_rejected') {
      return finalizeSequence({
        ...next,
        requestState: 'idle',
        reactionState: null,
        reactionTimestamp: null,
        actionStatusState: null,
        actionErrorCode: null,
        actionFailureStage: null,
        actionFailureMessagePublic: null,
        currentProcessId: null,
        currentActionId: null,
        currentCommandId: null,
        decisionLocked: false,
        isActionPhase: false,
      });
    }
    if (meta.stage === 'persist_final_state_failed') {
      return finalizeSequence({
        ...next,
        currentPhase: 'action',
        requestState: 'idle',
        reactionState: 'accepted',
        actionStatusState: 'error',
        actionErrorCode: meta.error_code,
        actionFailureStage: meta.stage,
        actionFailureMessagePublic: ev.data.error_message ?? '',
        decisionLocked: true,
        currentCommandId: meta.command_id,
        currentProcessId: null,
        currentActionId: meta.action_id ?? null,
        isActionStreamFinished: true,
        isActionPhase: false,
      });
    }
    if (meta.stage === 'start_failed' || meta.stage === 'running_failed') {
      return finalizeSequence({
        ...next,
        currentPhase: 'action',
        requestState: 'idle',
        reactionState: 'accepted',
        actionStatusState: 'error',
        actionErrorCode: meta.error_code,
        actionFailureStage: meta.stage,
        actionFailureMessagePublic: ev.data.error_message ?? '',
        currentProcessId: null,
        currentActionId: meta.action_id ?? null,
        isActionStreamFinished: true,
        decisionLocked: true,
        isActionPhase: false,
      });
    }
    return next;
  }

  if (ev.event === 'suggestion_reaction_committed') {
    const reaction = ev.data.reaction;
    const committedAt = ev.data.committed_at || next.reactionTimestamp;
    if (reaction === 'rejected') {
      return finalizeSequence({
        ...next,
        reactionState: 'rejected',
        reactionTimestamp: committedAt,
        dismissReason: ev.data.reason ?? null,
        actionStatusState: 'idle',
        requestState: 'idle',
        decisionLocked: true,
        currentCommandId: null,
        currentProcessId: null,
      });
    }
    return next;
  }

  if (ev.event === 'action_requested') {
    const acceptedAt = ev.data.accepted_at || ev.data.committed_at || next.reactionTimestamp;
    return finalizeSequence({
      ...next,
      currentPhase: 'suggestion',
      currentCommandId: eventCommandId ?? next.currentCommandId,
      currentActionId: ids.actionId ?? next.currentActionId,
      reactionState: 'accepted',
      reactionTimestamp: acceptedAt,
      actionStatusState: 'idle',
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      requestState: 'accepted_pending_start',
      decisionLocked: true,
      isActionPhase: false,
    });
  }

  if (ev.event === 'process_started') {
    const actionProcessEvent = isActionProcessStartedEvent(ev) ? ev : null;
    if (eventCommandId && next.currentCommandId && eventCommandId !== next.currentCommandId) {
      return next;
    }
    if (ids.processId) {
      next = { ...next, currentProcessId: ids.processId };
    }
    if (ids.actionId) {
      next = { ...next, currentActionId: ids.actionId };
    }
    if (actionProcessEvent) {
      const acceptedAt = actionProcessEvent.data.accepted_at || next.reactionTimestamp;
      return finalizeSequence({
        ...next,
        currentPhase: 'action',
        currentCommandId: eventCommandId ?? next.currentCommandId,
        currentActionId: ids.actionId ?? next.currentActionId,
        isActionStreamFinished: false,
        requestState: 'idle',
        reactionState: 'accepted',
        reactionTimestamp: acceptedAt,
        actionStatusState: 'processing',
        decisionLocked: true,
        isActionPhase: false,
      });
    }

    const sameSuggestion = Boolean(
      next.suggestionId && ids.suggestionId && next.suggestionId === ids.suggestionId
    );
    const decisionFinalized = Boolean(next.decisionLocked || next.reactionState !== null);
    if (sameSuggestion && decisionFinalized) {
      return next;
    }
    return finalizeSequence({
      ...next,
      currentPhase: 'suggestion',
      currentActionId: null,
      suggestionText: '',
      content: '',
      isSuggestionStreamFinished: false,
      reactionState: null,
      reactionTimestamp: null,
      actionStatusState: null,
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      interactionContract: null,
      requestState: 'idle',
      decisionLocked: false,
    });
  }

  if (ev.event === 'suggestion_chunk') {
    const chunk = ev.data.content;
    if (!chunk) return next;
    const contentCleared = next.content ? '' : next.content;
    return finalizeSequence({
      ...next,
      currentPhase: 'suggestion',
      content: contentCleared,
      suggestionText: appendChunk(next.suggestionText, chunk),
      isOverlayVisible: true,
    });
  }
  if (ev.event === 'completion_chunk') {
    const chunk = ev.data.content;
    if (!chunk) return next;
    if ((ids.kind ?? next.currentPhase) === 'action') {
      if (next.isActionStreamFinished) {
        return next;
      }
      return finalizeSequence(next);
    }
    let n = next;
    if (!n.isOverlayVisible) {
      n = { ...n, isOverlayVisible: true };
    }
    return finalizeSequence(n);
  }

  if (ev.event === 'process_paused') {
    if (
      isActionProcessPausedEvent(ev) &&
      ev.data.status === 'processing' &&
      ev.data.reason === 'approval_pending'
    ) {
      return finalizeSequence({
        ...next,
        requestState: 'idle',
        currentProcessId: null,
        currentPhase: 'action',
        currentCommandId: eventCommandId ?? next.currentCommandId,
        currentActionId: ids.actionId ?? next.currentActionId,
        isActionStreamFinished: false,
        reactionState: next.reactionState === null ? 'accepted' : next.reactionState,
        actionStatusState: 'processing',
        decisionLocked: true,
        isActionPhase: false,
      });
    }
    return finalizeSequence(next);
  }
  if (ev.event === 'process_completed') {
    const statusVal = ev.data.status;

    let n: AgentOverlayState = {
      ...next,
      requestState: 'idle',
      currentProcessId: null,
    };
    const kind = getLifecycleEventKind(ev) ?? ids.kind ?? n.currentPhase;
    if (kind === 'action' && isActionProcessCompletedEvent(ev)) {
      const reactionState = n.reactionState === null ? 'accepted' : n.reactionState;
      n = {
        ...n,
        currentPhase: 'action',
        currentCommandId: eventCommandId ?? n.currentCommandId,
        currentActionId: ids.actionId ?? n.currentActionId,
        isActionStreamFinished: true,
        reactionState,
        actionStatusState: statusVal,
        actionErrorCode: null,
        actionFailureStage: null,
        actionFailureMessagePublic: null,
        decisionLocked: true,
        isActionPhase: statusVal === 'success',
      };
    }
    if (kind === 'suggestion' && ev.data.kind === 'suggestion') {
      const interactionContract =
        ev.data.interaction_contract === 'action_offer' ||
        ev.data.interaction_contract === 'message_only'
          ? ev.data.interaction_contract
          : n.interactionContract;
      n = {
        ...n,
        isSuggestionStreamFinished: true,
        interactionContract,
        decisionLocked:
          interactionContract === 'message_only'
            ? true
            : n.reactionState !== null || n.decisionLocked,
      };
    }
    if (ids.processId && n.knownProcessIds.has(ids.processId)) {
      const s = new Set(n.knownProcessIds);
      s.delete(ids.processId);
      n = { ...n, knownProcessIds: s };
    }
    if (ids.actionId && n.knownActionIds.has(ids.actionId)) {
      const s = new Set(n.knownActionIds);
      s.delete(ids.actionId);
      n = { ...n, knownActionIds: s };
    }
    return finalizeSequence(n);
  }
  return next;
}
