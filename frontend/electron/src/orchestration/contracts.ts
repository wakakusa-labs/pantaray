import type { BrowserWindow } from 'electron';

import type { OverlayPlacements } from '../ipc/schemas/overlayPlacement';

import type {
  ActionDisplayStatus,
  OrchestrationServerEvent,
  OrchestrationStatus,
  SuggestionInteractionContract,
  UiLanguage,
} from './eventContracts';

export * from './eventContracts';

export type OverlayActionPhase =
  | 'idle'
  | 'requesting'
  | 'accepted_pending_start'
  | 'processing'
  | 'terminal';
export type OverlayActionStatus = ActionDisplayStatus;

export function isAcceptedIdleActionPhase(
  actionPhase: OverlayActionPhase | null | undefined
): actionPhase is 'accepted_pending_start' {
  return actionPhase === 'accepted_pending_start';
}

export function isReplayableActionPhase(
  actionPhase: OverlayActionPhase | null | undefined
): boolean {
  return actionPhase === 'requesting' || actionPhase === 'accepted_pending_start';
}

export function canReuseActionCommand(actionPhase: OverlayActionPhase | null | undefined): boolean {
  return (
    actionPhase === 'requesting' ||
    actionPhase === 'accepted_pending_start' ||
    actionPhase === 'processing'
  );
}

export type OverlaySnapshot = {
  suggestionId: string;
  commandId: string | null;
  interactionContract: SuggestionInteractionContract | null;
  suggestionText: string;
  reactionState: 'accepted' | 'rejected' | null;
  reactionTimestamp: string | null;
  actionPhase: OverlayActionPhase;
  actionStatus: OverlayActionStatus | null;
  actionErrorCode: string | null;
  actionFailureStage: string | null;
  actionFailureMessagePublic: string | null;
  processId: string | null;
  actionId: string | null;
  updatedAt: string | null;
  lastSequence: number;
  isLive: boolean;
};

export type OverlayInitialUiState = {
  expand?: boolean;
};

export type OverlaySnapshotPayload = {
  snapshot: OverlaySnapshot;
  initialUiState?: OverlayInitialUiState | null;
};

export type OverlayLiveResume = {
  kind: 'none' | 'suggestion' | 'action';
  processId: string | null;
  actionId: string | null;
  commandId: string | null;
  acceptedAt: string | null;
};

export type OverlayBootstrapResponse = {
  suggestionId: string;
  snapshot: OverlaySnapshot;
  lastSequence: number;
  liveResume: OverlayLiveResume;
};

export type ResumeProcessRequest = {
  kind?: 'suggestion' | 'action' | null;
  processId?: string | null;
  suggestionId?: string | null;
  actionId?: string | null;
  commandId?: string | null;
  fromStart?: boolean;
};

export type HistoryChangedPayload =
  | {
      source: 'orchestration_event';
      event?: OrchestrationServerEvent['event'] | null;
      suggestion_id?: string | null;
    }
  | { source: 'read_state' };

export type NotificationIpcHandlers = {
  onResizeNotificationWindow: (event: unknown, payload: unknown) => void;
  onNotificationActionAccept: (event: unknown, payload: unknown) => void;
  onNotificationActionReject: (event: unknown, payload: unknown) => void;
  onNotificationHide: (event: unknown) => void;
  onNotificationStopAction: (event: unknown) => void;
  onOverlayInteraction: (event: unknown) => void;
  onOverlayDragStart: (event: unknown, payload: unknown) => void;
  onOverlayDragMove: (event: unknown, payload: unknown) => void;
  onOverlayDragEnd: (event: unknown) => void;
  onHistoryOpenOverlay: (event: unknown, payload: unknown) => void;
};

export type CreateNotificationIpcHandlers = (options: {
  resumeLiveProcess: (payload: ResumeProcessRequest) => void;
  resolveOverlayBootstrap: (suggestionId: string) => Promise<OverlayBootstrapResponse | null>;
  refreshActionConversation: (actionId: string) => void;
  getMainWindow?: () => BrowserWindow | null;
}) => NotificationIpcHandlers;

export type NotificationWindowApi = {
  setLocalOwnerIdGetter: (getter: () => string | null) => void;
  // Synchronously destroy owner windows and invalidate pending delivery before rebinding.
  clearForOwnerChange: () => void;
  showNotification: (id: string) => void;
  isVisibleOverlayAtPoint?: (point: { x: number; y: number }) => boolean;
  hasRecentOverlayInteraction?: (referenceMs?: number) => boolean;
  sendToAllOverlays: (channel: string, payload: unknown) => void;
  sendResetToAllOverlays: (channel: string, payload: unknown) => void;
  hideOverlay?: (suggestionId: string) => void;
  sendToOverlay: (suggestionId: string, channel: string, payload: unknown) => void;
  setOverlaySnapshot: (suggestionId: string, payload: OverlaySnapshotPayload) => void;
  registerProcessAssociation: (processId: string, suggestionId: string) => void;
  registerActionAssociation: (actionId: string, overlayId: string) => void;
  // Binds unless an open window already shows the Action; for server-event-derived binds.
  adoptActionAssociation: (actionId: string, overlayId: string) => void;
  cleanupMappingsForProcess: (processId: string) => void;
  cleanupMappingsForAction: (actionId: string) => void;
  clearActionAssociations: () => void;
  resolveOverlayId: (args: {
    suggestionId?: string | null;
    processId?: string | null;
    actionId?: string | null;
  }) => string | null;
  resolveOverlayIdForSender: (sender: Readonly<{ id: number }>) => string | null;
  openStandaloneConversationOverlay: (
    overlayId: string,
    actionId: string | null
  ) => 'created' | 'loading' | 'focused';
  hasOverlayWindow: (overlayId: string) => boolean;
  destroyOverlayWindow: (overlayId: string) => void;
  dispatchEventToOverlay: (channel: string, payload: unknown) => boolean;
  setActionLiveSnapshotGetter: (getter: (actionId: string) => object | null) => void;
  setUiLanguageGetter?: (getter: () => UiLanguage) => void;
  setOverlayPlacementGetter: (getter: () => OverlayPlacements) => void;
  configureIpcWindowSecurity: (registration: {
    registerWindow: (role: 'overlay', sender: Readonly<{ id: number }>) => void;
    unregisterWindow: (sender: Readonly<{ id: number }>) => void;
  }) => void;
  createNotificationIpcHandlers?: CreateNotificationIpcHandlers;
};

export type OrchestrationStatusPayload = OrchestrationStatus;
