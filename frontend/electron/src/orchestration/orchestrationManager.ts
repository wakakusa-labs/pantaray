/**
 * Orchestration WS Manager（WebSocket 接続/再接続・認証・送信・resume）
 *
 * 目的:
 * - WS transport と renderer bridge を組み立て、接続・送信を所有する。
 *
 * NOTE:
 * - 実体の WS 実装は `electron/ws_orchestration.js`（CommonJS）を利用する。ここはオーケストレーション/DI の層。
 */

import type { BrowserWindow } from 'electron';

import type { ActionConversationPage } from '../actions/actionContracts';
import type { LocalRuntimeState } from '../auth/localRuntimeState';
import type { ScreenCaptureRequest } from '../capture/screenCapture';
import type {
  AcceptActionRequest,
  NotificationWindowApi,
  OverlaySnapshot,
  OrchestrationClientEvent,
  OrchestrationServerEvent,
  OrchestrationStatusPayload,
  ResumeProcessRequest,
} from './contracts';
import { isScreenCaptureRequestedEvent } from './eventContracts';
import {
  ActionFileAttachmentsSchema,
  ActionMessageRequestSchema,
  ActionProjectRefsSchema,
} from '../actions/actionContracts';
import { createOrchestrationRendererBridge } from './orchestrationRendererBridge';

export type CreateOrchestrationWS = (opts: {
  showNotification: NotificationWindowApi['showNotification'];
  forwardEventToRenderers: (msg: OrchestrationServerEvent) => void;
  forwardStatusToRenderers: (payload: OrchestrationStatusPayload) => void;
}) => OrchestratorLike;

type OrchestratorLike = {
  connect?: (url: string, headers: Record<string, string>) => void;
  disconnect?: () => void;
  send?: (message: OrchestrationClientEvent) => void;
  resumeProcess?: (args: ResumeProcessRequest) => boolean;
};

type LoggerLike = {
  error?: (name: string, payload?: unknown) => void;
};

type TimerApi = {
  setTimeout: typeof globalThis.setTimeout;
  clearTimeout: typeof globalThis.clearTimeout;
};

export type OrchestrationManager = {
  isConnected: () => boolean;
  ensureConnected: () => void;
  disconnect: () => void;
  /** Rebinds the socket after the owner changed; the URL carries the owner id. */
  reconnect: () => void;
  sendFromRenderer: (message: unknown) => Promise<void>;
  acceptAction: (request: AcceptActionRequest) => Promise<OverlaySnapshot | null>;
  enqueueResumeRequest: (req: ResumeProcessRequest) => void;
  getOverlaySnapshot: (suggestionId: string) => OverlaySnapshot | null;
  refreshActionConversation: (actionId: string) => void;
  refreshAndResumeActionConversation: (actionId: string) => void;
  handleApprovalDecisionSettled: (identity: {
    actionId: string;
    processId: string;
    approvalSessionId: string;
    toolRequestId: string;
  }) => string | null;
  resetActionLive: () => void;
};

function normalizeLocalhost(urlObj: URL): URL {
  try {
    if (urlObj.hostname === 'localhost') urlObj.hostname = '127.0.0.1';
  } catch {
    // no-op
  }
  return urlObj;
}

function getBackendWsOriginUrl(runtimeBackendUrl: string | null): URL | null {
  if (!runtimeBackendUrl || typeof runtimeBackendUrl !== 'string') {
    return null;
  }
  try {
    const u = new URL(runtimeBackendUrl);
    normalizeLocalhost(u);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    return u;
  } catch {
    return null;
  }
}

function buildOrchestrationUrl(runtimeBackendUrl: string | null, userId: string): string | null {
  const origin = getBackendWsOriginUrl(runtimeBackendUrl);
  if (!origin) return null;
  try {
    const full = new URL(`/v1/agents/users/${userId}/orchestrations`, origin);
    return full.toString();
  } catch (e) {
    console.error('Failed to build orchestration URL:', e);
    return null;
  }
}

export function createOrchestrationManager(params: {
  getRuntimeBackendUrl: () => string | null;
  notificationWindow: NotificationWindowApi;
  createOrchestrationWS: CreateOrchestrationWS;
  getMainWindow: () => BrowserWindow | null;
  getLocalApiToken: () => string | null;
  getOwnerId: () => string | null;
  getRuntimeState: () => LocalRuntimeState;
  getUiLanguage: () => 'en' | 'ja';
  readLatestActionConversationPage: (actionId: string) => Promise<ActionConversationPage>;
  /** Answers a screen capture request; absent means no desktop capture support. */
  respondToScreenCapture?: (request: ScreenCaptureRequest) => Promise<void>;
  onStatus?: (payload: OrchestrationStatusPayload) => void;
  logger?: LoggerLike | null;
  timers?: TimerApi;
}): OrchestrationManager {
  const notificationWindow = params.notificationWindow;
  if (
    typeof params.readLatestActionConversationPage !== 'function' ||
    typeof notificationWindow.resolveOverlayId !== 'function'
  ) {
    throw new Error('Action conversation read and Overlay target resolution are required');
  }

  let orchestrator: OrchestratorLike | null = null;
  let wsUrl: string | null = null;
  let wsHeaders: Record<string, string> = {};
  let connected = false;

  let pendingResumeRequests: ResumeProcessRequest[] = [];

  function normalizeId(value: unknown): string | null {
    if (value === null || value === undefined) return null;
    const normalized = String(value).trim();
    return normalized.length > 0 ? normalized : null;
  }

  function isRecord(value: unknown): value is Record<string, unknown> {
    return Boolean(value) && typeof value === 'object';
  }

  function isRuntimeReady(): boolean {
    try {
      return params.getRuntimeState().status === 'ready';
    } catch {
      return false;
    }
  }

  function requireOwner(): void {
    if (!isRuntimeReady() || !params.getOwnerId()) {
      throw new Error('Local owner is unavailable.');
    }
  }

  function parseClientEvent(message: unknown): OrchestrationClientEvent | null {
    if (!isRecord(message) || typeof message.event !== 'string' || !isRecord(message.data)) {
      return null;
    }
    const { event, data } = message;
    switch (event) {
      case 'execute_action': {
        const images = ActionMessageRequestSchema.shape.message.shape.images.safeParse(data.images);
        const projectRefs = ActionProjectRefsSchema.safeParse(data.supplement_project_refs);
        const files = ActionFileAttachmentsSchema.optional().safeParse(data.files);
        if (
          typeof data.suggestion_id !== 'string' ||
          typeof data.command_id !== 'string' ||
          (data.language !== 'en' && data.language !== 'ja') ||
          !(typeof data.supplement === 'string' || data.supplement === null) ||
          (data.approval_mode !== 'prompt_each_time' && data.approval_mode !== 'always_allow') ||
          !images.success ||
          !projectRefs.success ||
          !files.success
        ) {
          return null;
        }
        return {
          event,
          data: {
            suggestion_id: data.suggestion_id,
            command_id: data.command_id,
            language: data.language,
            supplement: data.supplement,
            supplement_project_refs: projectRefs.data,
            approval_mode: data.approval_mode,
            images: images.data,
            files: files.data,
          },
        };
      }
      case 'dismiss_suggestion':
      case 'reject_suggestion':
        if (typeof data.suggestion_id !== 'string') {
          return null;
        }
        return {
          event,
          data: {
            suggestion_id: data.suggestion_id,
            reason: typeof data.reason === 'string' ? data.reason : undefined,
          },
        };
      case 'stop_process':
        return typeof data.process_id === 'string'
          ? { event, data: { process_id: data.process_id } }
          : null;
      case 'resume_process':
        return {
          event,
          data: {
            kind: data.kind === 'action' || data.kind === 'suggestion' ? data.kind : undefined,
            process_id: typeof data.process_id === 'string' ? data.process_id : undefined,
            processId: typeof data.processId === 'string' ? data.processId : undefined,
            suggestion_id: typeof data.suggestion_id === 'string' ? data.suggestion_id : undefined,
            suggestionId: typeof data.suggestionId === 'string' ? data.suggestionId : undefined,
            action_id: typeof data.action_id === 'string' ? data.action_id : undefined,
            actionId: typeof data.actionId === 'string' ? data.actionId : undefined,
            command_id: typeof data.command_id === 'string' ? data.command_id : undefined,
            commandId: typeof data.commandId === 'string' ? data.commandId : undefined,
            fromStart: typeof data.fromStart === 'boolean' ? data.fromStart : undefined,
          },
        };
      case 'resume_session':
        if (
          typeof data.session_id !== 'string' ||
          !(typeof data.last_cursor === 'string' || data.last_cursor === null) ||
          typeof data.process_id !== 'string' ||
          typeof data.last_chunk_index !== 'number' ||
          (data.kind !== 'suggestion' && data.kind !== 'action')
        ) {
          return null;
        }
        return {
          event,
          data: {
            session_id: data.session_id,
            last_cursor: data.last_cursor,
            process_id: data.process_id,
            last_chunk_index: data.last_chunk_index,
            kind: data.kind,
            suggestion_id: typeof data.suggestion_id === 'string' ? data.suggestion_id : undefined,
            action_id: typeof data.action_id === 'string' ? data.action_id : undefined,
            command_id: typeof data.command_id === 'string' ? data.command_id : undefined,
          },
        };
      case 'ack_event':
        if (
          typeof data.session_id !== 'string' ||
          typeof data.process_id !== 'string' ||
          typeof data.event_id !== 'string'
        ) {
          return null;
        }
        return {
          event,
          data: {
            session_id: data.session_id,
            process_id: data.process_id,
            event_id: data.event_id,
          },
        };
      default:
        return null;
    }
  }

  function toResumeProcessRequest(message: OrchestrationClientEvent): ResumeProcessRequest | null {
    if (message.event !== 'resume_process') return null;
    return {
      kind: message.data.kind,
      processId: normalizeId(message.data.processId ?? message.data.process_id),
      suggestionId: normalizeId(message.data.suggestionId ?? message.data.suggestion_id),
      actionId: normalizeId(message.data.actionId ?? message.data.action_id),
      commandId: normalizeId(message.data.commandId ?? message.data.command_id),
      fromStart: message.data.fromStart,
    };
  }

  function buildResumeRequestKey(req: ResumeProcessRequest): string {
    return `${req.kind ?? ''}::${req.processId ?? ''}::${req.suggestionId ?? ''}::${req.actionId ?? ''}::${req.commandId ?? ''}`;
  }

  function resolveUrlAndHeaders(): { url: string; headers: Record<string, string> } | null {
    try {
      if (!isRuntimeReady()) {
        return null;
      }
      const token = params.getLocalApiToken();
      const userId = params.getOwnerId();
      if (!userId || !token) return null;
      const url = buildOrchestrationUrl(params.getRuntimeBackendUrl(), String(userId));
      if (!url) {
        console.error(
          'Failed to resolve orchestration WS URL: runtime backend URL is missing or invalid.'
        );
        return null;
      }
      const headers = { Authorization: `Bearer ${token}` };
      return { url, headers };
    } catch (e) {
      console.error('Failed to resolve orchestration WS URL/headers:', e);
      return null;
    }
  }

  function flushPendingResumeRequests(): void {
    if (!orchestrator || typeof orchestrator.resumeProcess !== 'function') return;
    if (!pendingResumeRequests.length) return;
    const remaining: ResumeProcessRequest[] = [];
    for (const req of pendingResumeRequests) {
      try {
        const success = Boolean(orchestrator.resumeProcess(req));
        if (!success) remaining.push(req);
      } catch (error) {
        console.error('resumeProcess failed:', error);
        remaining.push(req);
      }
    }
    pendingResumeRequests = remaining;
  }

  function enqueueResumeRequest(req: ResumeProcessRequest): void {
    const key = buildResumeRequestKey(req);
    if (
      pendingResumeRequests.some((existing) => {
        return buildResumeRequestKey(existing) === key;
      })
    ) {
      return;
    }
    pendingResumeRequests.push(req);
    flushPendingResumeRequests();
  }

  /**
   * A screen capture request is addressed to this process, not to a window: it
   * carries no conversation content and the answer goes back over HTTP, so it is
   * handled here and never reaches a renderer.
   */
  function forwardEventToRenderers(message: OrchestrationServerEvent): void {
    if (isScreenCaptureRequestedEvent(message)) {
      void params.respondToScreenCapture?.({
        captureRequestId: message.data.capture_request_id,
        actionId: message.data.action_id,
        processId: message.data.process_id,
        toolRequestId: message.data.tool_request_id,
        appName: message.data.app_name,
      });
      return;
    }
    rendererBridge.forwardEventToRenderers(message);
  }

  function ensureOrchestrator(): void {
    if (orchestrator) return;
    orchestrator = params.createOrchestrationWS({
      showNotification: notificationWindow.showNotification,
      forwardEventToRenderers,
      forwardStatusToRenderers: rendererBridge.forwardStatusToRenderers,
    });
  }

  function disconnect(): void {
    try {
      orchestrator?.disconnect?.();
    } catch (e) {
      console.error('WS disconnect failed:', e);
    } finally {
      connected = false;
      wsUrl = null;
      wsHeaders = {};
      rendererBridge.handleDisconnect();
    }
  }

  function ensureConnected(): void {
    try {
      ensureOrchestrator();
      const cfg = resolveUrlAndHeaders();
      if (cfg && cfg.url) {
        wsUrl = cfg.url;
        wsHeaders = cfg.headers || {};
        orchestrator?.connect?.(wsUrl, wsHeaders);
        flushPendingResumeRequests();
      } else {
        disconnect();
      }
    } catch (e) {
      console.error('Auto WS connect failed:', e);
    }
  }

  function reconnect(): void {
    disconnect();
    ensureConnected();
  }

  async function sendFromRenderer(message: unknown): Promise<void> {
    const clientMessage = parseClientEvent(message);
    if (!clientMessage) {
      throw new Error('Invalid orchestration client event payload.');
    }

    requireOwner();
    const resumeRequest = toResumeProcessRequest(clientMessage);
    if (resumeRequest) {
      enqueueResumeRequest(resumeRequest);
      return;
    }

    // Ensure orchestrator and send. Main owner 化したため、送信不能は fail-closed で surface する。
    let bootstrapped = false;
    if (!orchestrator) {
      const cfg = resolveUrlAndHeaders();
      if (!cfg || !cfg.url) {
        throw new Error('WS lazy-connect skipped: missing local API token or owner id');
      }
      wsUrl = cfg.url;
      wsHeaders = cfg.headers || {};
      ensureOrchestrator();
      bootstrapped = true;
    }
    if (bootstrapped && wsUrl) {
      try {
        orchestrator?.connect?.(wsUrl, wsHeaders);
        flushPendingResumeRequests();
      } catch (e) {
        console.error('WS bootstrap error:', e);
        throw e;
      }
    }

    try {
      if (!orchestrator || typeof orchestrator.send !== 'function') {
        throw new Error('orchestrator is unavailable');
      }
      orchestrator.send(clientMessage);
    } catch (e) {
      console.error('WS send error:', e);
      throw e;
    }
  }

  const rendererBridge = createOrchestrationRendererBridge({
    notificationWindow,
    getMainWindow: () => params.getMainWindow(),
    readLatestActionConversationPage: params.readLatestActionConversationPage,
    sendFromRenderer,
    requestResume: (request) => {
      // An internal refresh may finish after owner loss; its resume is obsolete.
      if (!isRuntimeReady() || !params.getOwnerId()) return;
      enqueueResumeRequest(request);
    },
    getUiLanguage: params.getUiLanguage,
    normalizeId,
    isConnected: () => connected,
    setConnected: (value) => {
      connected = value;
    },
    onStatus: (payload) => params.onStatus?.(payload),
    timers: params.timers,
  });

  return {
    isConnected: () => connected,
    ensureConnected,
    disconnect,
    reconnect,
    sendFromRenderer,
    acceptAction: rendererBridge.acceptAction,
    enqueueResumeRequest: (request) => {
      requireOwner();
      enqueueResumeRequest(request);
    },
    getOverlaySnapshot: rendererBridge.getOverlaySnapshot,
    refreshActionConversation: rendererBridge.refreshActionConversation,
    refreshAndResumeActionConversation: rendererBridge.refreshAndResumeActionConversation,
    handleApprovalDecisionSettled: rendererBridge.handleApprovalDecisionSettled,
    resetActionLive: () => {
      pendingResumeRequests = [];
      rendererBridge.resetActionLive();
    },
  };
}
