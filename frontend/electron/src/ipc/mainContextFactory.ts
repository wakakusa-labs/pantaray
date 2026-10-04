/**
 * MainContext factory（IPC handlers 用の依存性組み立て）
 *
 * 目的:
 * - `electron/src/main.ts` から MainContext 構築の塊を切り出し、main の責務を薄くする。
 * - IPC handler 側は `MainContext` のみに依存し、main の巨大化/密結合を抑える。
 */

import type { BrowserWindow } from 'electron';

import type { AuthState, CaptureGateState, MainContext, UiLanguage } from './context';
import type { ResumeProcessRequest } from '../orchestration/contracts';
import type { LocalRuntimeState } from '../auth/localRuntimeState';
import { INITIAL_LOCAL_RUNTIME_STATE } from '../auth/localRuntimeState';
import { broadcastUiLanguage, normalizeUiLanguage, saveUiLanguage } from '../ui/uiLanguage';
import type { CapturePrivacyManager } from '../privacy/capturePrivacy';
import { listInstalledAppOptions, type AppIconReader } from '../privacy/installedApps';
import type { RecordingStartResult, ScreenshotSyncManager } from '../screenshot/screenshotSync';
import type {
  SupabaseSessionInitializationStatus,
  SupabaseSessionManagerLike,
  SupabaseSessionState,
} from '../auth/contracts';
import { buildFrontendDevOrigin } from '../runtime/devFrontendEnv';
import { createIpcSenderSecurity } from './senderTrust';
import { restoreAndFocusWindow } from '../windows/windowVisibility';

export function buildMainContext(params: {
  ipcMain: MainContext['ipcMain'];
  aiConnection: MainContext['aiConnection'];
  getMainWindow: () => BrowserWindow | null;
  openNewConversationOverlay: MainContext['windows']['openNewConversationOverlay'];
  openActionConversationOverlay: MainContext['windows']['openActionConversationOverlay'];
  getAllWindows: () => BrowserWindow[];
  isDevRuntime: () => boolean;
  frontendDistIndex: string;
  recordSecurityEvent: (
    name: string,
    metadata: Readonly<Record<string, string | number | boolean | null>>,
    level: 'info' | 'warn'
  ) => void;

  // auth
  saveLoginState: (isLoggedIn: boolean) => void;
  getSupabaseSessionManager: () => SupabaseSessionManagerLike | null;
  getSupabaseSessionInitializationStatus?: () => SupabaseSessionInitializationStatus;
  getRuntimeState: () => LocalRuntimeState;
  signOut: () => Promise<void>;
  startBrowserLogin: MainContext['auth']['startBrowserLogin'];

  // history
  historyFetch: MainContext['history']['fetch'];
  markCompletionViewed: MainContext['history']['markCompletionViewed'];
  historyDeleteItem: MainContext['history']['deleteItem'];
  actionFiles: MainContext['actionFiles'];
  update: MainContext['update'];
  actions: MainContext['actions'];
  actionImages: MainContext['actionImages'];

  // ui language
  getUiSettingsPath: () => string;
  getUiLanguage: () => UiLanguage;
  setUiLanguage: (lang: UiLanguage) => void;
  getWorkspaceEditCommandPreference: MainContext['approval']['getWorkspaceEditCommandPreference'];
  setWorkspaceEditCommandPreference: MainContext['approval']['setWorkspaceEditCommandPreference'];
  getGlobalShortcutState: MainContext['shortcut']['getState'];
  setGlobalShortcutAccelerator: MainContext['shortcut']['setAccelerator'];
  workspaceSettingsGet: MainContext['workspaceSettings']['get'];
  workspaceSettingsGetReadAccessScope: MainContext['workspaceSettings']['getReadAccessScope'];
  workspaceSettingsGetCommandNetwork: MainContext['workspaceSettings']['getCommandNetwork'];
  workspaceSettingsUpdateCommandNetwork: MainContext['workspaceSettings']['updateCommandNetwork'];
  workspaceSettingsCreateOrganization: MainContext['workspaceSettings']['createOrganization'];
  workspaceSettingsCreateProject: MainContext['workspaceSettings']['createProject'];
  workspaceSettingsCreateFolder: MainContext['workspaceSettings']['createFolder'];
  workspaceSettingsReorderProjects: MainContext['workspaceSettings']['reorderProjects'];
  workspaceSettingsDeleteOrganization: MainContext['workspaceSettings']['deleteOrganization'];
  workspaceSettingsDeleteProject: MainContext['workspaceSettings']['deleteProject'];
  workspaceSettingsDeleteFolder: MainContext['workspaceSettings']['deleteFolder'];
  workspaceSettingsUpdateProjectLinks: MainContext['workspaceSettings']['updateProjectLinks'];
  workspaceSettingsUpdateFolderLinks: MainContext['workspaceSettings']['updateFolderLinks'];
  workspaceSettingsUpdateReadAccessScope: MainContext['workspaceSettings']['updateReadAccessScope'];
  workspaceSettingsSelectFolder: MainContext['workspaceSettings']['selectFolder'];

  // screenshot & privacy
  screenshotSync: ScreenshotSyncManager;
  capturePrivacy: CapturePrivacyManager;
  readAppIcon: AppIconReader;
  /** Wrapper around `screenshotSync.start` that opens what the gate deferred. */
  startScreenshots: () => Promise<RecordingStartResult>;
  readCaptureGateState: MainContext['screenshot']['getGateState'];
  /** Answers the recording screen with "later" and drops what it deferred. */
  dismissRecordingIntro: () => CaptureGateState;
  openCapturePermissionSettings: MainContext['screenshot']['openPermissionSettings'];
  onCaptureSettingsChanged?: () => void;

  // external
  openExternalUrl: MainContext['externalUrl']['open'];
  wsSend: MainContext['ws']['send'];
  wsAcceptAction: MainContext['ws']['acceptAction'];
  wsGetStatus: MainContext['ws']['getStatus'];

  // overlay
  enqueueResumeRequest: (payload: ResumeProcessRequest) => void;
  resolveOverlayBootstrap: MainContext['overlay']['resolveOverlayBootstrap'];
  submitApprovalDecision: MainContext['overlay']['submitApprovalDecision'];
  getActionApprovalMode: MainContext['overlay']['getActionApprovalMode'];
  setActionApprovalMode: MainContext['overlay']['setActionApprovalMode'];
  createNotificationIpcHandlers?: MainContext['overlay']['createNotificationIpcHandlers'];
}): MainContext {
  const normalizeOptionalString = (value: unknown): string | null =>
    typeof value === 'string' && value.trim().length > 0 ? value : null;
  const buildAuthState = (
    state: SupabaseSessionState | null | undefined,
    runtimeState: LocalRuntimeState
  ): AuthState => {
    const isLoggedIn = Boolean(state?.isLoggedIn);
    return {
      authStatus: state?.authStatus ?? (isLoggedIn ? 'authenticated' : 'unauthenticated'),
      isLoggedIn,
      user: state?.user ?? null,
      runtimeState,
    };
  };
  const buildEmptyAuthState = (
    authStatus: AuthState['authStatus'],
    runtimeState: LocalRuntimeState
  ): AuthState => ({
    authStatus,
    isLoggedIn: false,
    user: null,
    runtimeState,
  });

  const security = createIpcSenderSecurity({
    getMainWindow: params.getMainWindow,
    isDevRuntime: params.isDevRuntime,
    frontendDevOrigin: params.isDevRuntime() ? buildFrontendDevOrigin() : null,
    frontendDistIndex: params.frontendDistIndex,
    recordSecurityEvent: params.recordSecurityEvent,
  });

  function requireOwner(): string {
    const state = params.getRuntimeState();
    if (state.status !== 'ready' || !state.owner) throw new Error('Local owner is unavailable.');
    return state.owner.id;
  }

  async function updatePrivacy<T>(update: () => T): Promise<T> {
    const ownerId = requireOwner();
    const assertOwner = () => {
      if (requireOwner() !== ownerId) throw new Error('Local owner changed.');
    };
    const result = await params.screenshotSync.updatePrivacy(() => {
      // An owner transition cancels this write, but is not a recorder failure.
      const state = params.getRuntimeState();
      if (state.status !== 'ready' || state.owner?.id !== ownerId) {
        return { kind: 'cancelled' } as const;
      }
      return { kind: 'updated', value: update() } as const;
    });
    if (result.kind === 'cancelled') throw new Error('Local owner changed.');
    assertOwner();
    const settings = params.capturePrivacy.getCaptureSettings();
    const mainWindow = params.getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      try {
        mainWindow.webContents.send('privacy:captureSettingsUpdated', settings);
      } catch (error) {
        // Persistence succeeded; a closing renderer cannot roll it back.
        console.error('Failed to notify the renderer of saved privacy settings:', error);
      }
    }
    params.onCaptureSettingsChanged?.();
    return result.value;
  }

  return {
    ipcMain: params.ipcMain,
    aiConnection: params.aiConnection,
    security,
    windows: {
      getMainWindow: params.getMainWindow,
      openNewConversationOverlay: params.openNewConversationOverlay,
      openActionConversationOverlay: params.openActionConversationOverlay,
      showMainRoute: (route) => {
        const mainWindow = params.getMainWindow();
        if (!mainWindow) return;
        try {
          const url = params.isDevRuntime()
            ? `${buildFrontendDevOrigin()}/#${route}`
            : `file://${params.frontendDistIndex}#${route}`;
          // A failed load is reported and retried by the window's own did-fail-load listener.
          void mainWindow.loadURL(url).catch(() => undefined);
          restoreAndFocusWindow(mainWindow);
        } catch {
          // no-op
        }
      },
    },

    auth: {
      saveLoginState: (isLoggedIn) => params.saveLoginState(Boolean(isLoggedIn)),
      getState: () => {
        try {
          const mgr = params.getSupabaseSessionManager();
          if (!mgr) {
            const initializationStatus =
              params.getSupabaseSessionInitializationStatus?.() ?? 'initializing';
            const authStatus =
              initializationStatus === 'initializing' ? 'initializing' : 'unauthenticated';
            return buildEmptyAuthState(authStatus, params.getRuntimeState());
          }
          const state = mgr?.getState?.();
          return buildAuthState(state, params.getRuntimeState());
        } catch {
          return buildEmptyAuthState('initializing', INITIAL_LOCAL_RUNTIME_STATE);
        }
      },
      signOut: async () => {
        try {
          await params.signOut();
          return { ok: true };
        } catch (e) {
          return { ok: false, error: e instanceof Error ? e.message : String(e) };
        }
      },
      startBrowserLogin: async (route) => params.startBrowserLogin(route),
    },

    history: {
      fetch: async (p) => params.historyFetch(p),
      markCompletionViewed: params.markCompletionViewed,
      deleteItem: params.historyDeleteItem,
    },

    actionFiles: params.actionFiles,
    update: params.update,

    actions: params.actions,

    actionImages: params.actionImages,

    ui: {
      getLanguage: () => normalizeUiLanguage(params.getUiLanguage()),
      setLanguage: (lang) => {
        requireOwner();
        const next = normalizeUiLanguage(lang);
        saveUiLanguage(params.getUiSettingsPath(), next);
        params.setUiLanguage(next);
        broadcastUiLanguage(params.getAllWindows(), next);
        return next;
      },
    },

    approval: {
      getWorkspaceEditCommandPreference: async () => params.getWorkspaceEditCommandPreference(),
      setWorkspaceEditCommandPreference: async (approvalMode) =>
        params.setWorkspaceEditCommandPreference(approvalMode),
    },

    shortcut: {
      getState: params.getGlobalShortcutState,
      setAccelerator: params.setGlobalShortcutAccelerator,
    },

    workspaceSettings: {
      get: async () => params.workspaceSettingsGet(),
      getReadAccessScope: async () => params.workspaceSettingsGetReadAccessScope(),
      getCommandNetwork: async () => params.workspaceSettingsGetCommandNetwork(),
      updateCommandNetwork: async (enabled) =>
        params.workspaceSettingsUpdateCommandNetwork(enabled),
      createOrganization: async (input) => params.workspaceSettingsCreateOrganization(input),
      createProject: async (input) => params.workspaceSettingsCreateProject(input),
      createFolder: async (input) => params.workspaceSettingsCreateFolder(input),
      reorderProjects: async (input) => params.workspaceSettingsReorderProjects(input),
      deleteOrganization: async (organizationId) =>
        params.workspaceSettingsDeleteOrganization(organizationId),
      deleteProject: async (projectId) => params.workspaceSettingsDeleteProject(projectId),
      deleteFolder: async (folderId) => params.workspaceSettingsDeleteFolder(folderId),
      updateProjectLinks: async (projectId, input) =>
        params.workspaceSettingsUpdateProjectLinks(projectId, input),
      updateFolderLinks: async (folderId, input) =>
        params.workspaceSettingsUpdateFolderLinks(folderId, input),
      updateReadAccessScope: async (readAccessScope) =>
        params.workspaceSettingsUpdateReadAccessScope(readAccessScope),
      selectFolder: async () => params.workspaceSettingsSelectFolder(),
    },

    screenshot: {
      getStatus: () => {
        requireOwner();
        return params.screenshotSync.getStatus();
      },
      start: async () => {
        requireOwner();
        return params.startScreenshots();
      },
      stop: async () => {
        requireOwner();
        return params.screenshotSync.stop();
      },
      getGateState: () => params.readCaptureGateState(),
      // The screen belongs to the owner it was shown to, and a start it is waiting on
      // can sit in System Settings long enough for that owner to change. Answering on
      // that late reply would drop the conversation the current owner is waiting on, so
      // only the current owner's answer is taken. Any other just reads the gate, which is
      // the read the current owner's own screen makes (it can open their waiting conversation).
      dismissIntro: (ownerId) =>
        requireOwner() === ownerId ? params.dismissRecordingIntro() : params.readCaptureGateState(),
      openPermissionSettings: () => params.openCapturePermissionSettings(),
    },

    privacy: {
      getCaptureSettings: () => {
        requireOwner();
        return params.capturePrivacy.getCaptureSettings();
      },
      updateCaptureSettings: (next) =>
        updatePrivacy(() => params.capturePrivacy.updateCaptureSettings(next)),
      listInstalledApps: () => listInstalledAppOptions(params.readAppIcon),
      getIdeFileRules: () => {
        requireOwner();
        return params.capturePrivacy.getIdeFileRules();
      },
      setCaptureEditing: async (request) => {
        if (request.kind === 'begin' && requireOwner() !== request.ownerId) {
          throw new Error('Local owner changed.');
        }
        return params.screenshotSync.setCaptureEditing(request);
      },
      updateIdeFileRules: (nextRules) =>
        updatePrivacy(() => params.capturePrivacy.updateIdeFileRules(nextRules)),
    },

    externalUrl: { open: async (rawUrl) => params.openExternalUrl(rawUrl) },
    ws: {
      send: async (message) => params.wsSend(message),
      acceptAction: async (request) => params.wsAcceptAction(request),
      getStatus: () => params.wsGetStatus(),
    },

    overlay: {
      resumeLiveProcess: (payload) => {
        params.enqueueResumeRequest({
          kind:
            payload.kind === 'action' || payload.kind === 'suggestion' ? payload.kind : undefined,
          processId: normalizeOptionalString(payload.processId),
          suggestionId: normalizeOptionalString(payload.suggestionId),
          actionId: normalizeOptionalString(payload.actionId),
          commandId: normalizeOptionalString(payload.commandId),
          fromStart: payload.fromStart !== false,
        });
      },
      resolveOverlayBootstrap: async (suggestionId) =>
        params.resolveOverlayBootstrap(String(suggestionId)),
      submitApprovalDecision: async (payload) => params.submitApprovalDecision(payload),
      getActionApprovalMode: async (actionId) => params.getActionApprovalMode(actionId),
      setActionApprovalMode: async (actionId, approvalMode) =>
        params.setActionApprovalMode(actionId, approvalMode),
      createNotificationIpcHandlers: params.createNotificationIpcHandlers,
    },
  };
}
