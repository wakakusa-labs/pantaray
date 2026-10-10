import { randomUUID } from 'crypto';

import type { BrowserWindow } from 'electron';

import type { ActionConversationPage } from '../actions/actionContracts';
import {
  createActionLiveCoordinator,
  type ActionLiveEventRoute,
  type ActionLiveUpdate,
} from '../actions/actionLiveCore';
import type {
  AcceptActionRequest,
  ExecuteActionClientEvent,
  NotificationWindowApi,
  OverlaySnapshot,
  OrchestrationServerEvent,
  OrchestrationStatusPayload,
  ResumeProcessRequest,
} from './contracts';
import { canReuseActionCommand, isReplayableActionPhase } from './contracts';
import { createSuggestionRecords } from './suggestionRecords';
import { broadcastHistoryChanged } from './historyNotifications';
import {
  getEventMeta,
  isProcessCompletedActionEvent,
  isProcessPausedActionEvent,
  isProcessStartedActionEvent,
  isProcessStartedSuggestionEvent,
  isSuggestionReactionCommittedEvent,
} from './eventContracts';
import { applyOverlayServerEvent, createOverlaySnapshot } from './overlayState';

const ACTION_COMMAND_PRESTART_REPLAY_INTERVAL_MS = 2_000;
type TimerApi = {
  setTimeout: typeof globalThis.setTimeout;
  clearTimeout: typeof globalThis.clearTimeout;
};

export function createOrchestrationRendererBridge(params: {
  notificationWindow: NotificationWindowApi;
  getMainWindow: () => BrowserWindow | null;
  readLatestActionConversationPage: (actionId: string) => Promise<ActionConversationPage>;
  sendFromRenderer: (message: unknown) => Promise<void>;
  requestResume: (request: ResumeProcessRequest) => void;
  getUiLanguage: () => 'en' | 'ja';
  normalizeId: (value: unknown) => string | null;
  isConnected: () => boolean;
  setConnected: (value: boolean) => void;
  onStatus?: (payload: OrchestrationStatusPayload) => void;
  timers?: TimerApi;
}) {
  const notificationWindow = params.notificationWindow;
  const timers = params.timers || globalThis;

  let lastReplayedSessionId: string | null = null;
  let prestartReplayTimer: ReturnType<typeof setTimeout> | null = null;
  const suggestionRecords = createSuggestionRecords({
    notificationWindow,
    getMainWindow: params.getMainWindow,
    onRecordsChanged: () => syncPrestartReplayLoop(),
  });
  const liveProcesses = new Map<string, string>();

  function sendActionLiveUpdate(update: ActionLiveUpdate): void {
    const mainWin = params.getMainWindow();
    if (mainWin && !mainWin.isDestroyed()) {
      try {
        mainWin.webContents.send('action:conversationUpdated', update);
      } catch (error) {
        console.error('Failed to deliver Action conversation to main window:', error);
      }
    }
    if (update.kind === 'reset') {
      notificationWindow.sendResetToAllOverlays('action:conversationUpdated', update);
      return;
    }
    const overlayId = notificationWindow.resolveOverlayId({ actionId: update.snapshot.actionId });
    if (overlayId) {
      try {
        notificationWindow.sendToOverlay(overlayId, 'action:conversationUpdated', update);
      } catch (error) {
        console.error('Failed to deliver Action conversation to Overlay:', error);
      }
    }
    const status = update.snapshot.page?.action.status ?? null;
    if (status && status !== 'queued' && status !== 'processing') {
      for (const [processId, actionId] of liveProcesses) {
        if (actionId !== update.snapshot.actionId) continue;
        liveProcesses.delete(processId);
        notificationWindow.cleanupMappingsForProcess(processId);
      }
      notificationWindow.cleanupMappingsForAction(update.snapshot.actionId);
    }
  }

  const actionLive = createActionLiveCoordinator({
    readLatestPage: params.readLatestActionConversationPage,
    publish: sendActionLiveUpdate,
    surfaceRefreshError: (actionId, error) => {
      console.error(`Failed to refresh Action conversation ${actionId}:`, error);
    },
  });
  notificationWindow.setActionLiveSnapshotGetter(actionLive.getSnapshot);

  function syncCommandStateFromEvent(
    message: OrchestrationServerEvent,
    route: ActionLiveEventRoute
  ): void {
    let stateEvent: OrchestrationServerEvent | null = message;
    if (route.legacyDisposition === 'suppress') {
      stateEvent =
        isProcessStartedActionEvent(message) || isProcessCompletedActionEvent(message)
          ? message
          : null;
    }
    if (!stateEvent) return;
    const meta = getEventMeta(stateEvent);
    const data = typeof stateEvent.data === 'object' && stateEvent.data ? stateEvent.data : {};
    const suggestionId = params.normalizeId(
      meta?.suggestion_id ?? (data as Record<string, unknown>).suggestion_id
    );
    if (!suggestionId) return;
    const next = applyOverlayServerEvent(suggestionRecords.getSnapshot(suggestionId), stateEvent);
    if (isSuggestionReactionCommittedEvent(stateEvent) && stateEvent.data.reaction === 'rejected') {
      suggestionRecords.dismiss(next);
      return;
    }
    suggestionRecords.storeSnapshot(next);
  }

  function hasOutstandingPrestartCommands(): boolean {
    return Array.from(suggestionRecords.values()).some(
      (record) =>
        record.executeEnvelope !== null && isReplayableActionPhase(record.snapshot.actionPhase)
    );
  }

  function clearPrestartReplayLoop(): void {
    if (prestartReplayTimer === null) return;
    timers.clearTimeout(prestartReplayTimer);
    prestartReplayTimer = null;
  }

  function syncPrestartReplayLoop(): void {
    if (!params.isConnected() || !hasOutstandingPrestartCommands()) {
      clearPrestartReplayLoop();
      return;
    }
    if (prestartReplayTimer !== null) return;
    prestartReplayTimer = timers.setTimeout(() => {
      prestartReplayTimer = null;
      void replayOutstandingCommands().finally(syncPrestartReplayLoop);
    }, ACTION_COMMAND_PRESTART_REPLAY_INTERVAL_MS);
  }

  async function replayOutstandingCommands(): Promise<void> {
    for (const record of suggestionRecords.values()) {
      if (!record.executeEnvelope || !isReplayableActionPhase(record.snapshot.actionPhase))
        continue;
      try {
        await params.sendFromRenderer(record.executeEnvelope);
      } catch (error) {
        console.error('Failed to replay outstanding action command:', error);
      }
    }
  }

  function extractRoutingIds(message: OrchestrationServerEvent): {
    suggestionId: string | null;
    processId: string | null;
    actionId: string | null;
  } {
    const meta = getEventMeta(message);
    switch (message.event) {
      case 'suggestion_chunk':
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id),
          processId: params.normalizeId(meta?.process_id),
          actionId: null,
        };
      case 'suggestion_reaction_committed':
      case 'action_requested':
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id ?? message.data.suggestion_id),
          processId: null,
          actionId: null,
        };
      case 'process_started':
        if (isProcessStartedActionEvent(message)) {
          return {
            suggestionId: params.normalizeId(meta?.suggestion_id ?? message.data.suggestion_id),
            processId: params.normalizeId(meta?.process_id ?? message.data.process_id),
            actionId: params.normalizeId(meta?.action_id ?? message.data.action_id),
          };
        }
        if (!isProcessStartedSuggestionEvent(message)) {
          return { suggestionId: null, processId: null, actionId: null };
        }
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id ?? message.data.suggestion_id),
          processId: params.normalizeId(meta?.process_id ?? message.data.process_id),
          actionId: null,
        };
      case 'process_completed':
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id ?? message.data.suggestion_id),
          processId: params.normalizeId(meta?.process_id ?? message.data.process_id),
          actionId: isProcessCompletedActionEvent(message)
            ? params.normalizeId(meta?.action_id ?? message.data.action_id)
            : null,
        };
      case 'process_paused':
        if (isProcessPausedActionEvent(message)) {
          return {
            suggestionId: params.normalizeId(meta?.suggestion_id ?? message.data.suggestion_id),
            processId: params.normalizeId(meta?.process_id ?? message.data.process_id),
            actionId: params.normalizeId(meta?.action_id ?? message.data.action_id),
          };
        }
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id),
          processId: params.normalizeId(meta?.process_id),
          actionId: null,
        };
      case 'completion_chunk':
      case 'error':
        return {
          suggestionId: params.normalizeId(meta?.suggestion_id),
          processId: params.normalizeId(meta?.process_id),
          actionId: params.normalizeId(meta?.action_id),
        };
      default:
        return { suggestionId: null, processId: null, actionId: null };
    }
  }

  function forwardEventToRenderers(msg: OrchestrationServerEvent): void {
    const { suggestionId, processId, actionId } = extractRoutingIds(msg);

    if (isProcessStartedActionEvent(msg) && actionId) {
      if (processId) liveProcesses.set(processId, actionId);
    }
    try {
      if (msg.event === 'process_started') {
        if (processId && suggestionId) {
          notificationWindow.registerProcessAssociation(processId, suggestionId);
        }
        if (actionId && suggestionId) {
          notificationWindow.adoptActionAssociation(actionId, suggestionId);
        }
      }
    } catch {
      // no-op
    }

    const liveResult = actionLive.handleEvent(msg);
    syncCommandStateFromEvent(msg, liveResult.route);
    if (
      msg.event === 'suggestion_reaction_committed' ||
      msg.event === 'action_requested' ||
      (msg.event === 'process_started' &&
        (msg.data.kind ?? getEventMeta(msg)?.kind) === 'action') ||
      isProcessPausedActionEvent(msg) ||
      msg.event === 'process_completed'
    ) {
      broadcastHistoryChanged(params.getMainWindow(), {
        source: 'orchestration_event',
        event: msg.event,
        suggestion_id: suggestionId,
      });
    }

    let handled = false;
    if (liveResult.route.legacyDisposition === 'pass') {
      try {
        handled = notificationWindow.dispatchEventToOverlay('ws:event', msg);
      } catch (error) {
        console.error('Failed to dispatch WS event to overlay:', error);
      }
    }

    if (!handled && suggestionId && liveResult.route.legacyDisposition === 'pass') {
      try {
        notificationWindow.sendToOverlay(suggestionId, 'ws:event', msg);
      } catch (error) {
        console.error('Failed to send WS event to overlay by suggestion_id:', error);
      }
    }

    const mainWin = params.getMainWindow();
    if (liveResult.route.legacyDisposition === 'pass' && mainWin && !mainWin.isDestroyed()) {
      try {
        mainWin.webContents.send('ws:event', msg);
      } catch {
        // no-op
      }
    }

    if (msg.event === 'process_completed') {
      if (processId) liveProcesses.delete(processId);
      try {
        if (processId) notificationWindow.cleanupMappingsForProcess(processId);
      } catch {
        // no-op
      }
    }
  }

  function forwardStatusToRenderers(payload: OrchestrationStatusPayload): void {
    try {
      notificationWindow.sendToAllOverlays('ws:status', payload);
    } catch {
      // no-op
    }
    const mainWin = params.getMainWindow();
    if (mainWin && !mainWin.isDestroyed()) {
      try {
        mainWin.webContents.send('ws:status', payload);
      } catch {
        // no-op
      }
    }
    try {
      const status = payload.status;
      if (status === 'connected') {
        params.setConnected(true);
        syncPrestartReplayLoop();
      } else if (status === 'closed' || status === 'error') {
        params.setConnected(false);
        handleDisconnect();
      } else if (status === 'session_started') {
        params.setConnected(true);
        const sessionId = params.normalizeId(payload.session_id);
        if (sessionId && sessionId !== lastReplayedSessionId) {
          lastReplayedSessionId = sessionId;
          void replayOutstandingCommands();
          // The auth-token reconnect force-clears the transport's process
          // registry, so live action processes (HTTP-started ones included)
          // have no other re-attach path. liveProcesses owns their lifecycle
          // (registered on process_started, released on process_completed or
          // terminal status); a duplicate resume is idempotent backend-side.
          for (const [processId, actionId] of liveProcesses) {
            params.requestResume({ kind: 'action', processId, actionId, fromStart: false });
          }
        }
        syncPrestartReplayLoop();
      } else if (status === 'session_resumed') {
        params.setConnected(true);
        const actionId = liveProcesses.get(params.normalizeId(payload.process_id) ?? '');
        if (actionId && payload.missing)
          void actionLive.handleStreamBoundary({ kind: 'gap', actionId });
      } else if (status === 'resume_requested') {
        const actionId = params.normalizeId(payload.action_id);
        if (actionId) void actionLive.handleStreamBoundary({ kind: 'reconnect', actionId });
      }
    } catch {
      // no-op
    }
    try {
      params.onStatus?.(payload);
    } catch {
      // no-op
    }
  }

  function handleDisconnect(): void {
    lastReplayedSessionId = null;
    clearPrestartReplayLoop();
  }

  function resetActionLive(): void {
    for (const processId of liveProcesses.keys()) {
      notificationWindow.cleanupMappingsForProcess(processId);
    }
    notificationWindow.clearActionAssociations();
    liveProcesses.clear();
    suggestionRecords.reset();
    clearPrestartReplayLoop();
    actionLive.clearAll();
  }

  function refreshActionConversation(actionId: string): void {
    void actionLive.refresh(actionId);
  }

  function hasLiveActionProcess(actionId: string): boolean {
    for (const liveActionId of liveProcesses.values()) {
      if (liveActionId === actionId) return true;
    }
    return false;
  }

  // Opening a conversation from history only reads a REST snapshot, so a run that
  // this session never attached to (an app restart leaves liveProcesses empty)
  // would deliver no lifecycle, step, or approval events. A nonterminal page
  // therefore resumes the Action root process the latest run names: the backend
  // resolves that resume from durable process events, not from the WS session
  // that started the run.
  function refreshAndResumeActionConversation(actionId: string): void {
    void (async () => {
      await actionLive.refresh(actionId);
      const action = actionLive.getSnapshot(actionId)?.page?.action;
      if (action?.status !== 'queued' && action?.status !== 'processing') return;
      if (action.latest_run_id === null || hasLiveActionProcess(actionId)) return;
      params.requestResume({
        kind: 'action',
        actionId,
        processId: action.latest_run_id,
        fromStart: true,
      });
    })();
  }

  function handleApprovalDecisionSettled(identity: {
    actionId: string;
    processId: string;
    approvalSessionId: string;
    toolRequestId: string;
  }): string | null {
    const rootProcessId = actionLive.handleApprovalDecisionSettled(identity);
    refreshActionConversation(identity.actionId);
    return rootProcessId;
  }

  async function acceptAction(request: AcceptActionRequest): Promise<OverlaySnapshot | null> {
    const normalizedSuggestionId = params.normalizeId(request.suggestionId);
    if (!normalizedSuggestionId) return null;

    const current = suggestionRecords.getRecord(normalizedSuggestionId);
    const shouldReuse = Boolean(current && canReuseActionCommand(current.snapshot.actionPhase));
    const currentCommandId = shouldReuse ? params.normalizeId(current?.snapshot.commandId) : null;
    const requestedCommandId = params.normalizeId(request.commandId);
    const reusableEnvelope =
      currentCommandId && current?.executeEnvelope?.data.command_id === currentCommandId
        ? current.executeEnvelope
        : null;
    if (
      (requestedCommandId && requestedCommandId !== currentCommandId) ||
      (currentCommandId && !reusableEnvelope)
    ) {
      throw new Error('Action execute envelope is unavailable for this command.');
    }
    const nextCommandId = currentCommandId ?? randomUUID();
    const executeEnvelope: ExecuteActionClientEvent = reusableEnvelope
      ? reusableEnvelope
      : {
          event: 'execute_action',
          data: {
            suggestion_id: normalizedSuggestionId,
            command_id: nextCommandId,
            language: params.getUiLanguage(),
            supplement: request.supplement,
            supplement_project_refs: request.supplementProjectRefs,
            approval_mode: request.approvalMode,
            images: request.images,
            files: request.files,
          },
        };
    const nextSnapshot: OverlaySnapshot =
      current && current.snapshot.actionPhase !== 'terminal'
        ? {
            ...current.snapshot,
            commandId: nextCommandId,
            actionPhase: shouldReuse ? current.snapshot.actionPhase : 'requesting',
            updatedAt: new Date().toISOString(),
            isLive: true,
          }
        : createOverlaySnapshot(normalizedSuggestionId, nextCommandId, {
            actionPhase: 'requesting',
            isLive: true,
          });

    suggestionRecords.store({ snapshot: nextSnapshot, executeEnvelope });
    try {
      await params.sendFromRenderer(executeEnvelope);
    } catch (error) {
      if (current) suggestionRecords.store(current);
      else suggestionRecords.clear(normalizedSuggestionId);
      throw error;
    }
    return suggestionRecords.getSnapshot(normalizedSuggestionId);
  }

  return {
    acceptAction,
    adoptSuggestionSnapshot: suggestionRecords.adoptPersisted,
    forwardEventToRenderers,
    forwardStatusToRenderers,
    getOverlaySnapshot: suggestionRecords.getSnapshot,
    refreshActionConversation,
    refreshAndResumeActionConversation,
    handleApprovalDecisionSettled,
    handleDisconnect,
    resetActionLive,
  };
}
