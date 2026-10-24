import type { AgentOverlayAction, AgentOverlayState, OverlaySnapshotPayload } from './overlayTypes';
import { isAcceptedIdleActionPhase, isAcceptedIdleRequestState } from './overlayTypes';
import { applySuggestionId, normalizeId, reduceServerEvent } from './serverEventReducer';

function normalizeActionStatus(value: unknown): AgentOverlayState['actionStatusState'] {
  const normalized = normalizeId(value)?.toLowerCase() ?? null;
  if (!normalized) {
    return null;
  }
  if (KNOWN_ACTION_STATUSES.has(normalized)) {
    return normalized as AgentOverlayState['actionStatusState'];
  }
  return 'error';
}

const ACTION_TERMINAL_STATUSES = new Set(['success', 'error', 'timeout', 'canceled']);
const KNOWN_ACTION_STATUSES = new Set([
  'idle',
  'processing',
  'success',
  'error',
  'timeout',
  'canceled',
]);
function getSnapshotPhaseRankFromState(state: AgentOverlayState): number {
  if (state.actionStatusState && ACTION_TERMINAL_STATUSES.has(state.actionStatusState)) {
    return 3;
  }
  if (state.actionStatusState === 'processing') {
    return 2;
  }
  if (isAcceptedIdleRequestState(state.requestState)) {
    return 1;
  }
  if (state.requestState === 'requesting') {
    return 0;
  }
  return -1;
}

function getSnapshotPhaseRank(
  phase: OverlaySnapshotPayload['snapshot']['actionPhase'],
  terminalStatus: string | null
): number {
  if (phase === 'terminal') {
    return terminalStatus ? 3 : -1;
  }
  if (phase === 'processing') {
    return 2;
  }
  if (isAcceptedIdleActionPhase(phase)) {
    return 1;
  }
  if (phase === 'requesting') {
    return 0;
  }
  return -1;
}

export function createInitialAgentOverlayState(): AgentOverlayState {
  return {
    suggestionId: null,
    currentProcessId: null,
    currentActionId: null,
    currentCommandId: null,
    currentPhase: null,
    lastSequence: 0,

    content: '',
    suggestionText: '',
    reactionState: null,
    reactionTimestamp: null,
    dismissReason: null,
    artifacts: [],
    actionStatusState: null,
    actionErrorCode: null,
    actionFailureStage: null,
    actionFailureMessagePublic: null,
    interactionContract: null,
    requestState: 'idle',
    decisionLocked: false,
    isSuggestionStreamFinished: false,
    isActionStreamFinished: false,

    isOverlayVisible: false,
    isActionPhase: false,

    historyFooterOverride: null,
    historyExpandOverride: null,
    isExpanded: true,

    knownProcessIds: new Set<string>(),
    knownActionIds: new Set<string>(),
  };
}

export function reduceAgentOverlayState(
  state: AgentOverlayState,
  action: AgentOverlayAction
): AgentOverlayState {
  switch (action.type) {
    case 'SET_SUGGESTION_ID': {
      const suggestionId = normalizeId(action.suggestionId);
      if (!suggestionId) return state;
      if (state.suggestionId === suggestionId) return state;
      const reset = action.resetKnownIds !== false;
      return {
        ...state,
        suggestionId,
        ...(reset ? { knownProcessIds: new Set<string>(), knownActionIds: new Set<string>() } : {}),
      };
    }
    case 'SET_CURRENT_PROCESS_ID': {
      const processId = normalizeId(action.processId);
      if (state.currentProcessId === processId) return state;
      return { ...state, currentProcessId: processId };
    }
    case 'SET_REQUEST_STATE': {
      if (state.requestState === action.value) return state;
      return { ...state, requestState: action.value };
    }
    case 'SET_DECISION_LOCKED': {
      if (state.decisionLocked === action.value) return state;
      return { ...state, decisionLocked: action.value };
    }
    case 'SET_EXPANDED': {
      if (state.isExpanded === action.value) return state;
      return { ...state, isExpanded: action.value };
    }
    case 'TOGGLE_EXPAND': {
      const nextExpanded = !state.isExpanded;
      return {
        ...state,
        historyExpandOverride: nextExpanded,
        isExpanded: nextExpanded,
      };
    }
    case 'HYDRATE_SNAPSHOT': {
      const snapshot = action.payload.snapshot;
      const suggestionId = normalizeId(snapshot.suggestionId);
      if (!suggestionId) return state;
      if (snapshot.lastSequence < state.lastSequence) return state;
      const phase = snapshot.actionPhase;
      const commandId = normalizeId(snapshot.commandId);
      const acceptedAt = normalizeId(snapshot.reactionTimestamp);
      const processId = normalizeId(snapshot.processId);
      const actionId = normalizeId(snapshot.actionId);
      const terminalStatus = normalizeActionStatus(snapshot.actionStatus);
      const suggestionText = snapshot.suggestionText;
      const isSuggestionFinishedSnapshot = Boolean(
        !snapshot.isLive || snapshot.interactionContract || phase !== 'idle'
      );
      const expandOverride =
        typeof action.payload.initialUiState?.expand === 'boolean'
          ? action.payload.initialUiState.expand
          : null;

      let next: AgentOverlayState = {
        ...applySuggestionId(state, suggestionId),
        suggestionId,
        currentCommandId: commandId,
        lastSequence: snapshot.lastSequence,
        artifacts: snapshot.artifacts,
      };

      const incomingRank = getSnapshotPhaseRank(phase, terminalStatus);
      const currentRank =
        commandId && state.currentCommandId === commandId
          ? getSnapshotPhaseRankFromState(state)
          : -1;
      if (incomingRank >= 0 && currentRank > incomingRank) {
        return {
          ...next,
          currentPhase: state.currentPhase,
          currentProcessId: state.currentProcessId,
          currentActionId: state.currentActionId,
          content: state.content,
          suggestionText: state.suggestionText,
          reactionState: state.reactionState,
          reactionTimestamp: state.reactionTimestamp,
          actionStatusState: state.actionStatusState,
          actionErrorCode: state.actionErrorCode,
          actionFailureStage: state.actionFailureStage,
          actionFailureMessagePublic: state.actionFailureMessagePublic,
          interactionContract: state.interactionContract,
          requestState: state.requestState,
          decisionLocked: state.decisionLocked,
          isSuggestionStreamFinished: state.isSuggestionStreamFinished,
          isActionStreamFinished: state.isActionStreamFinished,
          isOverlayVisible: true,
          isActionPhase: state.isActionPhase,
          historyFooterOverride: state.historyFooterOverride,
          historyExpandOverride: state.historyExpandOverride,
          isExpanded: state.isExpanded,
          knownProcessIds: state.knownProcessIds,
          knownActionIds: state.knownActionIds,
        };
      }

      if (processId && !next.knownProcessIds.has(processId)) {
        const knownProcessIds = new Set(next.knownProcessIds);
        knownProcessIds.add(processId);
        next = { ...next, knownProcessIds };
      }
      if (actionId && !next.knownActionIds.has(actionId)) {
        const knownActionIds = new Set(next.knownActionIds);
        knownActionIds.add(actionId);
        next = { ...next, knownActionIds };
      }

      if (phase === 'requesting') {
        return {
          ...next,
          currentPhase: 'suggestion',
          currentProcessId: null,
          currentActionId: actionId,
          content: '',
          suggestionText: suggestionText || next.suggestionText,
          reactionState: null,
          reactionTimestamp: null,
          actionStatusState: null,
          actionErrorCode: null,
          actionFailureStage: null,
          actionFailureMessagePublic: null,
          interactionContract: snapshot.interactionContract,
          requestState: 'requesting',
          decisionLocked: true,
          isActionPhase: false,
          isSuggestionStreamFinished: true,
          isOverlayVisible: true,
          historyExpandOverride: expandOverride,
          isExpanded: expandOverride !== null ? expandOverride : next.isExpanded,
        };
      }
      if (isAcceptedIdleActionPhase(phase)) {
        return {
          ...next,
          currentPhase: 'suggestion',
          currentProcessId: null,
          currentActionId: actionId,
          content: '',
          suggestionText: suggestionText || next.suggestionText,
          reactionState: 'accepted',
          reactionTimestamp: acceptedAt,
          actionStatusState: 'idle',
          actionErrorCode: snapshot.actionErrorCode,
          actionFailureStage: snapshot.actionFailureStage,
          actionFailureMessagePublic: snapshot.actionFailureMessagePublic,
          interactionContract: snapshot.interactionContract,
          requestState: phase,
          decisionLocked: true,
          isActionPhase: false,
          isSuggestionStreamFinished: true,
          isOverlayVisible: true,
          historyExpandOverride: expandOverride,
          isExpanded: expandOverride !== null ? expandOverride : next.isExpanded,
        };
      }
      if (phase === 'processing') {
        return {
          ...next,
          currentPhase: 'action',
          currentProcessId: processId,
          currentActionId: actionId,
          content: '',
          suggestionText: suggestionText || next.suggestionText,
          reactionState: 'accepted',
          reactionTimestamp: acceptedAt,
          actionStatusState: 'processing',
          actionErrorCode: null,
          actionFailureStage: null,
          actionFailureMessagePublic: null,
          interactionContract: snapshot.interactionContract,
          requestState: 'idle',
          decisionLocked: true,
          isActionPhase: false,
          isSuggestionStreamFinished: true,
          isActionStreamFinished: false,
          isOverlayVisible: true,
          historyExpandOverride: expandOverride,
          isExpanded: expandOverride !== null ? expandOverride : next.isExpanded,
        };
      }
      if (phase === 'terminal') {
        return {
          ...next,
          currentPhase: 'action',
          currentProcessId: null,
          currentActionId: actionId,
          content: '',
          suggestionText: suggestionText || next.suggestionText,
          reactionState: 'accepted',
          reactionTimestamp: acceptedAt,
          actionStatusState: terminalStatus,
          actionErrorCode: snapshot.actionErrorCode,
          actionFailureStage: snapshot.actionFailureStage,
          actionFailureMessagePublic: snapshot.actionFailureMessagePublic,
          interactionContract: snapshot.interactionContract,
          requestState: 'idle',
          decisionLocked: true,
          isActionPhase: terminalStatus === 'success',
          isSuggestionStreamFinished: true,
          isActionStreamFinished: true,
          isOverlayVisible: true,
          historyExpandOverride: expandOverride,
          isExpanded: expandOverride !== null ? expandOverride : next.isExpanded,
        };
      }
      return {
        ...next,
        currentPhase: 'suggestion',
        currentActionId: actionId,
        content: '',
        suggestionText,
        reactionState: snapshot.reactionState,
        reactionTimestamp: snapshot.reactionTimestamp,
        dismissReason: snapshot.dismissReason,
        actionStatusState: snapshot.actionStatus,
        actionErrorCode: snapshot.actionErrorCode,
        actionFailureStage: snapshot.actionFailureStage,
        actionFailureMessagePublic: snapshot.actionFailureMessagePublic,
        interactionContract: snapshot.interactionContract,
        requestState: 'idle',
        decisionLocked:
          snapshot.interactionContract === 'message_only' ||
          snapshot.reactionState !== null ||
          state.decisionLocked,
        isSuggestionStreamFinished: isSuggestionFinishedSnapshot,
        isOverlayVisible: true,
        historyExpandOverride: expandOverride,
        isExpanded: expandOverride !== null ? expandOverride : next.isExpanded,
      };
    }
    case 'SET_CONTENT_TEXT': {
      const text = String(action.text ?? '');
      if (state.suggestionText)
        return state.isOverlayVisible ? state : { ...state, isOverlayVisible: true };
      return { ...state, content: text, isOverlayVisible: true };
    }
    case 'SERVER_EVENT':
      return reduceServerEvent(state, action.event);
    default:
      return state;
  }
}
