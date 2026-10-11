import type {
  ActionLiveMeta,
  ActionErrorMeta,
  OrchestrationServerEvent,
  SuggestionInteractionContract,
} from '@/types/websocket';

export type OverlayPhase = 'suggestion' | 'action' | null;
export type OverlayRequestState = 'idle' | 'requesting' | 'accepted_pending_start';
export type OverlayActionStatusState =
  | 'idle'
  | 'processing'
  | 'success'
  | 'error'
  | 'canceled'
  | 'timeout';

export function isAcceptedIdleRequestState(
  requestState: OverlayRequestState | null | undefined
): requestState is 'accepted_pending_start' {
  return requestState === 'accepted_pending_start';
}

export function isAcceptedIdleActionPhase(
  actionPhase: OverlaySnapshot['actionPhase'] | null | undefined
): actionPhase is 'accepted_pending_start' {
  return actionPhase === 'accepted_pending_start';
}

/**
 * Orchestration WebSocket から届くイベント（renderer側で扱う拡張形）。
 *
 * NOTE:
 * - サーバは `meta` を付与する（process_id / suggestion_id / action_id / kind など）。
 * - canonical な WS 契約は `src/types/websocket.ts` が所有し、overlay 側ではそのまま使う。
 */
export type OverlayServerEvent = OrchestrationServerEvent;

export function isActionErrorMeta(meta: unknown): meta is ActionErrorMeta {
  if (!meta || typeof meta !== 'object') return false;
  const candidate = meta as Partial<ActionErrorMeta>;
  return (
    candidate.kind === 'action' &&
    typeof candidate.suggestion_id === 'string' &&
    typeof candidate.command_id === 'string' &&
    typeof candidate.stage === 'string' &&
    typeof candidate.error_code === 'string'
  );
}

export function isActionLiveMeta(meta: unknown): meta is ActionLiveMeta {
  if (!meta || typeof meta !== 'object') return false;
  const candidate = meta as Partial<ActionLiveMeta>;
  return (
    candidate.kind === 'action' &&
    typeof candidate.suggestion_id === 'string' &&
    typeof candidate.process_id === 'string' &&
    typeof candidate.action_id === 'string'
  );
}

export type OverlaySnapshot = {
  suggestionId: string;
  commandId: string | null;
  interactionContract: SuggestionInteractionContract | null;
  suggestionText: string;
  reactionState: 'accepted' | 'rejected' | null;
  reactionTimestamp: string | null;
  dismissReason: string | null;
  actionPhase: 'idle' | 'requesting' | 'accepted_pending_start' | 'processing' | 'terminal';
  actionStatus: 'idle' | 'processing' | 'success' | 'error' | 'canceled' | 'timeout' | null;
  actionErrorCode: string | null;
  actionFailureStage: string | null;
  actionFailureMessagePublic: string | null;
  processId: string | null;
  actionId: string | null;
  updatedAt: string | null;
  lastSequence: number;
  isLive: boolean;
};

export type OverlaySnapshotPayload = {
  snapshot: OverlaySnapshot;
  initialUiState?: {
    expand?: boolean;
  } | null;
};

/**
 * AgentOverlay の状態（WSイベント由来の状態遷移を中心に保持する）。
 *
 * NOTE:
 * - UIアニメーション等（遅延fade）は controller 側で扱う。
 */
export type AgentOverlayState = {
  suggestionId: string | null;
  currentProcessId: string | null;
  currentActionId: string | null;
  currentCommandId: string | null;
  currentPhase: OverlayPhase;
  lastSequence: number;

  // text
  content: string;
  suggestionText: string;
  // status
  reactionState: 'accepted' | 'rejected' | null;
  reactionTimestamp: string | null;
  /** What the user wrote when they dismissed it. */
  dismissReason: string | null;
  actionStatusState: OverlayActionStatusState | null;
  actionErrorCode: string | null;
  actionFailureStage: string | null;
  actionFailureMessagePublic: string | null;
  interactionContract: SuggestionInteractionContract | null;
  requestState: OverlayRequestState;
  /**
   * decision（Accept/Dismiss）を受け付けるかどうかのロック。
   *
   * 方針:
   * - ユーザーがボタンを押した瞬間に true にし、UI上はボタン自体を非表示にする（連打防止）。
   * - 同一 suggestion_id で pending 以外へ遷移した後に pending に戻すことはしない。
   */
  decisionLocked: boolean;
  isSuggestionStreamFinished: boolean;
  isActionStreamFinished: boolean;

  // UI flags driven by streams
  isOverlayVisible: boolean;
  isActionPhase: boolean;

  // history overrides
  historyFooterOverride: boolean | null;
  historyExpandOverride: boolean | null;
  isExpanded: boolean;

  // filtering
  knownProcessIds: Set<string>;
  knownActionIds: Set<string>;
};

export type AgentOverlayAction =
  | { type: 'SERVER_EVENT'; event: OverlayServerEvent }
  | { type: 'HYDRATE_SNAPSHOT'; payload: OverlaySnapshotPayload }
  | { type: 'SET_CONTENT_TEXT'; text: string }
  | { type: 'TOGGLE_EXPAND' }
  | { type: 'SET_REQUEST_STATE'; value: OverlayRequestState }
  | { type: 'SET_DECISION_LOCKED'; value: boolean }
  | { type: 'SET_EXPANDED'; value: boolean }
  | { type: 'SET_SUGGESTION_ID'; suggestionId: string | null; resetKnownIds?: boolean }
  | { type: 'SET_CURRENT_PROCESS_ID'; processId: string | null };
