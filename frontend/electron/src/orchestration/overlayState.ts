import type {
  ActionErrorMeta,
  OverlaySnapshot,
  OverlaySnapshotPayload,
  OrchestrationServerEvent,
} from './contracts';
import {
  getEventMeta,
  isActionRequestedEvent,
  isErrorEvent,
  isProcessCompletedActionEvent,
  isProcessCompletedSuggestionEvent,
  isProcessPausedActionEvent,
  isProcessStartedActionEvent,
  isProcessStartedSuggestionEvent,
  isSuggestionChunkEvent,
  isSuggestionReactionCommittedEvent,
} from './eventContracts';

const KNOWN_ACTION_STATUSES = new Set([
  'idle',
  'processing',
  'success',
  'error',
  'canceled',
  'timeout',
]);

export type PersistedProcessEventRow = {
  event_id: string;
  suggestion_id: string;
  action_id?: string | null;
  user_id: string;
  sequence: number;
  event_name: string;
  payload: unknown;
  created_at?: string | null;
};

function normalizeId(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function normalizeActionStatus(value: unknown): OverlaySnapshot['actionStatus'] {
  const normalized = normalizeId(value)?.toLowerCase() ?? null;
  if (!normalized) {
    return null;
  }
  if (KNOWN_ACTION_STATUSES.has(normalized)) {
    return normalized as OverlaySnapshot['actionStatus'];
  }
  return 'error';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

function isActionErrorMeta(meta: unknown): meta is ActionErrorMeta {
  return (
    isRecord(meta) &&
    meta.kind === 'action' &&
    typeof meta.suggestion_id === 'string' &&
    typeof meta.command_id === 'string' &&
    typeof meta.stage === 'string' &&
    typeof meta.error_code === 'string'
  );
}

function getEventSequence(event: OrchestrationServerEvent): number {
  return typeof event.sequence === 'number' && Number.isFinite(event.sequence) ? event.sequence : 0;
}

function appendLiveText(previous: string, chunk: string): string {
  if (!previous) return chunk;
  return `${previous}\n${chunk}`;
}

export function createOverlaySnapshot(
  suggestionId: string,
  commandId: string | null,
  overrides: Partial<OverlaySnapshot> = {}
): OverlaySnapshot {
  return {
    suggestionId,
    commandId,
    interactionContract: null,
    suggestionText: '',
    reactionState: null,
    reactionTimestamp: null,
    dismissReason: null,
    actionPhase: 'idle',
    actionStatus: null,
    actionErrorCode: null,
    actionFailureStage: null,
    actionFailureMessagePublic: null,
    processId: null,
    actionId: null,
    updatedAt: null,
    lastSequence: 0,
    isLive: true,
    ...overrides,
  };
}

export function toOverlaySnapshotPayload(
  snapshot: OverlaySnapshot,
  initialUiState?: OverlaySnapshotPayload['initialUiState']
): OverlaySnapshotPayload {
  return { snapshot, initialUiState };
}

export function deserializeProcessEventRow(
  row: PersistedProcessEventRow
): OrchestrationServerEvent {
  if (!isRecord(row.payload) || !isRecord(row.payload.data)) {
    throw new Error('Persisted process event payload must contain a data object');
  }
  const envelope: Record<string, unknown> = {
    event: row.event_name,
    event_id: row.event_id,
    sequence: row.sequence,
    data: row.payload.data,
  };
  if (isRecord(row.payload.meta)) {
    envelope.meta = row.payload.meta;
  }
  return envelope as OrchestrationServerEvent;
}

export function buildOverlaySnapshotFromEvents(
  suggestionId: string,
  events: readonly OrchestrationServerEvent[]
): OverlaySnapshot {
  let current = createOverlaySnapshot(suggestionId, null, { isLive: false });
  for (const event of events) {
    current = applyOverlayServerEvent(current, event);
  }
  return current;
}

export function applyOverlayServerEvent(
  snapshot: OverlaySnapshot | null,
  event: OrchestrationServerEvent
): OverlaySnapshot {
  const meta = getEventMeta(event);
  const data: Record<string, unknown> = isRecord(event.data) ? event.data : {};
  const suggestionId =
    normalizeId(meta?.suggestion_id ?? data.suggestion_id) ?? snapshot?.suggestionId;
  if (!suggestionId) {
    throw new Error(`Overlay event ${event.event} is missing suggestion_id`);
  }
  const current = snapshot ?? createOverlaySnapshot(suggestionId, null);
  const sequence = getEventSequence(event);
  if (sequence > 0 && sequence <= current.lastSequence) {
    return current;
  }
  const nextSequence = sequence > 0 ? sequence : current.lastSequence;
  const nextUpdatedAt = normalizeId(data.updated_at) ?? new Date().toISOString();

  switch (event.event) {
    case 'suggestion_chunk':
      if (!isSuggestionChunkEvent(event)) return current;
      return {
        ...current,
        suggestionId,
        suggestionText: appendLiveText(current.suggestionText, event.data.content),
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: true,
      };

    case 'suggestion_reaction_committed':
      if (!isSuggestionReactionCommittedEvent(event)) return current;
      if (event.data.reaction !== 'rejected') return current;
      return {
        ...current,
        suggestionId,
        reactionState: event.data.reaction,
        reactionTimestamp: normalizeId(event.data.committed_at) ?? current.reactionTimestamp,
        dismissReason: event.data.reason ?? null,
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: false,
      };

    case 'action_requested':
      if (!isActionRequestedEvent(event)) return current;
      return {
        ...current,
        suggestionId,
        commandId: normalizeId(event.data.command_id ?? meta?.command_id) ?? current.commandId,
        reactionState: 'accepted',
        reactionTimestamp:
          normalizeId(event.data.accepted_at) ??
          normalizeId(event.data.committed_at) ??
          current.reactionTimestamp,
        actionPhase: 'accepted_pending_start',
        actionStatus: 'idle',
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: false,
      };

    case 'process_started':
      if (isProcessStartedActionEvent(event)) {
        const acceptedAt = normalizeId(event.data.accepted_at) ?? current.reactionTimestamp;
        return {
          ...current,
          suggestionId,
          commandId: normalizeId(event.data.command_id ?? meta?.command_id) ?? current.commandId,
          reactionState: 'accepted',
          reactionTimestamp: acceptedAt,
          actionPhase: 'processing',
          actionStatus: 'processing',
          processId: normalizeId(event.data.process_id ?? meta?.process_id),
          actionId: normalizeId(event.data.action_id ?? meta?.action_id),
          updatedAt: nextUpdatedAt,
          lastSequence: nextSequence,
          isLive: true,
        };
      }
      if (!isProcessStartedSuggestionEvent(event)) return current;
      return {
        ...current,
        suggestionId,
        processId: normalizeId(event.data.process_id ?? meta?.process_id),
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: true,
      };

    case 'completion_chunk':
      if (getEventMeta(event)?.kind === 'action') {
        return {
          ...current,
          suggestionId,
          updatedAt: nextUpdatedAt,
          lastSequence: nextSequence,
          isLive: true,
        };
      }
      return current;

    case 'process_paused': {
      if (!isProcessPausedActionEvent(event)) return current;
      if (event.data.reason !== 'approval_pending') return current;
      return {
        ...current,
        suggestionId,
        actionPhase: 'processing',
        actionStatus: 'processing',
        processId: normalizeId(event.data.process_id ?? meta?.process_id) ?? current.processId,
        actionId: normalizeId(event.data.action_id ?? meta?.action_id) ?? current.actionId,
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: false,
      };
    }

    case 'process_completed': {
      if (isProcessCompletedSuggestionEvent(event)) {
        return {
          ...current,
          suggestionId,
          interactionContract:
            event.data.interaction_contract === 'action_offer' ||
            event.data.interaction_contract === 'message_only'
              ? event.data.interaction_contract
              : current.interactionContract,
          updatedAt: nextUpdatedAt,
          lastSequence: nextSequence,
          isLive: false,
        };
      }
      if (!isProcessCompletedActionEvent(event)) return current;
      const terminalStatus = normalizeActionStatus(event.data.status) ?? current.actionStatus;
      return {
        ...current,
        suggestionId,
        actionPhase: 'terminal',
        actionStatus: terminalStatus,
        actionErrorCode: null,
        actionFailureStage: null,
        actionFailureMessagePublic: null,
        processId: normalizeId(event.data.process_id ?? meta?.process_id) ?? current.processId,
        actionId: normalizeId(event.data.action_id ?? meta?.action_id) ?? current.actionId,
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: false,
      };
    }

    case 'error': {
      if (!isErrorEvent(event) || !isActionErrorMeta(meta)) {
        return {
          ...current,
          updatedAt: nextUpdatedAt,
          lastSequence: nextSequence,
          isLive: false,
        };
      }
      if (meta.stage === 'preflight_rejected') {
        return {
          ...current,
          suggestionId,
          commandId: null,
          reactionState: null,
          reactionTimestamp: null,
          actionPhase: 'idle',
          actionStatus: null,
          processId: null,
          updatedAt: nextUpdatedAt,
          lastSequence: nextSequence,
          isLive: false,
        };
      }
      return {
        ...current,
        suggestionId,
        processId: normalizeId(meta.process_id) ?? current.processId,
        actionId: normalizeId(meta.action_id) ?? current.actionId,
        updatedAt: nextUpdatedAt,
        lastSequence: nextSequence,
        isLive: true,
      };
    }

    default:
      return current;
  }
}
