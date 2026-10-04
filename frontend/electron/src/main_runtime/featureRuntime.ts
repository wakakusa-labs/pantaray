import { BrowserWindow, app, dialog, globalShortcut, ipcMain, nativeImage, shell } from 'electron';
import type { createAuthCoordinator } from '../auth/authCoordinator';
import type { createLocalBackendAuthContextController } from '../auth/localBackendAuthContextController';
import type { LocalOwner } from '../auth/localRuntimeState';
import type { createSupabaseWiring } from '../auth/supabaseWiring';
import { saveUserState } from '../auth/userState';
import { createScreenCaptureResponder } from '../capture/screenCaptureRuntime';
import { createActionFetcher } from '../actions/actionFetch';
import { createActionLatestPageReader } from '../actions/actionLatestPageReader';
import { createActionApprovalDecisionFetcher } from '../actions/actionApprovalDecisionFetch';
import { createActionApprovalModeFetcher } from '../actions/actionApprovalModeFetch';
import { submitActionApprovalDecision } from '../actions/actionApprovalSubmission';
import { createActionReadState } from '../history/actionReadState';
import { broadcastHistoryChanged } from '../orchestration/historyNotifications';
import { createHistoryFetcher } from '../history/historyFetch';
import { createHistoryItemDeleter } from '../history/historyItemDelete';
import type { MainContext } from '../ipc/context';
import { createOverlayBootstrapFetcher } from '../history/overlayBootstrapFetch';
import { startMainIpcRuntime } from './ipcBootstrap';
import { createLocalBackendClient } from '../localBackend/client';
import type { NotificationWindowApi, UiLanguage } from '../orchestration/contracts';
import {
  createOrchestrationManager,
  type CreateOrchestrationWS,
} from '../orchestration/orchestrationManager';
import { readAppIconDataUrl } from '../privacy/appIcon';
import { createCapturePrivacyManager } from '../privacy/capturePrivacy';
import { resolveInstalledAppBundleId } from '../privacy/installedApps';
import { createExternalUrlOpener } from '../security/openExternalUrl';
import {
  createScreenshotSyncManager,
  type RecordingStartResult,
} from '../screenshot/screenshotSync';
import {
  holdsCaptureOsPermissions,
  openCapturePermissionSettings,
  requestCapturePermissions,
} from '../windows/macosPermissions';
import { createConversationOverlayOwner } from '../windows/conversationOverlay';
import { presentRecordingIntro } from '../windows/recordingIntroWindow';
import {
  createApprovalPreferenceFetcher,
  type ApprovalMode,
} from '../settings/approvalPreferencesFetch';
import { createWorkspaceSettingsFetcher } from '../settings/workspaceSettingsFetch';
import { broadcastUiLanguage, loadUiLanguage } from '../ui/uiLanguage';
import { getWelcomeSuggestionText } from '../ui/mainProcessCopy';
import type { DesktopRuntime } from './desktopRuntime';
import { postWelcomeSuggestion } from './welcomeSuggestion';
import {
  createGlobalShortcutController,
  createGlobalShortcutStore,
} from './globalShortcutController';
import type { createUpdateUiManager } from './updateUi';
type LoggerLike = NonNullable<Parameters<typeof createExternalUrlOpener>[0]['logger']> & {
  event?: (name: string, payload: unknown, level?: string) => void;
  error?: (name: string, payload?: unknown) => void;
};

const MACOS_INITIAL_GLOBAL_SHORTCUT = 'Option+Space';
type FeatureRuntimeParams = {
  aiConnection: MainContext['aiConnection'];
  frontendDistIndex: string;
  localArtifactRoot: string;
  isDevRuntime: () => boolean;
  desktopRuntime: DesktopRuntime;
  notificationWindow: NotificationWindowApi;
  createOrchestrationWS: CreateOrchestrationWS;
  authCoordinator: ReturnType<typeof createAuthCoordinator>;
  localBackendAuthContextController: ReturnType<typeof createLocalBackendAuthContextController>;
  supabaseWiring: ReturnType<typeof createSupabaseWiring>;
  /** Bearer credential of the running local helper. Never reaches a renderer. */
  getLocalApiToken: () => string | null;
  updateUi: ReturnType<typeof createUpdateUiManager>;
  getMainWindow: () => BrowserWindow | null;
  createMainWindow: () => void;
  resolveUiSettingsPath: (userId: string | null) => string;
  getUiLanguage: () => UiLanguage;
  setUiLanguage: (language: UiLanguage) => void;
  logger: LoggerLike | null;
};
const ACTION_CONVERSATION_LATEST_PAGE_LIMIT = 25;

function safely<T>(operation: () => T, fallback: T): T {
  try {
    return operation();
  } catch {
    return fallback;
  }
}

export function createDesktopFeatureRuntime(params: FeatureRuntimeParams) {
  let settingsScopeUserId: string | null = null;
  params.notificationWindow.setLocalOwnerIdGetter(params.supabaseWiring.getLocalOwnerId);
  const conversationOverlay = createConversationOverlayOwner({
    getRuntimeState: params.supabaseWiring.getRuntimeState,
    openOverlay: params.notificationWindow.openStandaloneConversationOverlay,
    destroyOverlay: params.notificationWindow.destroyOverlayWindow,
    bindActionToOverlay: params.notificationWindow.registerActionAssociation,
    resolveOverlayIdForAction: (actionId) =>
      params.notificationWindow.resolveOverlayId({ actionId }),
    hasOverlayWindow: params.notificationWindow.hasOverlayWindow,
    refreshAndResumeConversation: (actionId) =>
      orchestration.refreshAndResumeActionConversation(actionId),
    holdsCaptureOsPermissions,
    // The main window is where the recording screen lives, and it reads the pending
    // request set just above — so a window the user closed is created rather than
    // sent to, and one already in front is told to re-read it.
    presentRecordingIntro: () =>
      presentRecordingIntro({
        getMainWindow: params.getMainWindow,
        createMainWindow: params.createMainWindow,
      }),
  });
  const shortcutController = createGlobalShortcutController({
    registry: globalShortcut,
    store: createGlobalShortcutStore(app.getPath('userData')),
    initialAccelerator: process.platform === 'darwin' ? MACOS_INITIAL_GLOBAL_SHORTCUT : null,
    onShortcut: () => {
      try {
        conversationOverlay.openNewConversationOverlay();
      } catch (error) {
        params.logger?.error?.('NEW_CONVERSATION_SHORTCUT_ERR', { err: error });
      }
    },
  });
  const resolveOverlayBootstrap = async (suggestionId: string) => {
    const normalizedSuggestionId = String(suggestionId || '').trim();
    return normalizedSuggestionId ? overlayBootstrapFetch(normalizedSuggestionId) : null;
  };
  const latestActionPageReader = createActionLatestPageReader((actionId) =>
    actions.readConversationPage({
      actionId,
      cursor: null,
      limit: ACTION_CONVERSATION_LATEST_PAGE_LIMIT,
    })
  );
  const orchestration = createOrchestrationManager({
    getRuntimeBackendUrl: params.desktopRuntime.getBackendUrl,
    notificationWindow: params.notificationWindow,
    createOrchestrationWS: params.createOrchestrationWS,
    getMainWindow: params.getMainWindow,
    getLocalApiToken: params.getLocalApiToken,
    getOwnerId: params.supabaseWiring.getLocalOwnerId,
    getRuntimeState: params.supabaseWiring.getRuntimeState,
    getUiLanguage: params.getUiLanguage,
    readLatestActionConversationPage: latestActionPageReader.read,
    // `requestJson` is defined just below; the responder only calls it once a
    // capture request actually arrives, long after this module finished loading.
    respondToScreenCapture: createScreenCaptureResponder({
      getCaptureSettings: () => capturePrivacy.getCaptureSettings(),
      isCaptureEditing: () => screenshotSync.getCaptureEditing(),
      getCurrentSubjectId: () => getUserId(),
      localArtifactRoot: params.localArtifactRoot,
      postAnswer: (body) =>
        requestJson({ method: 'POST', path: '/local/action-screen-capture', body }),
      logger: params.logger,
    }),
    logger: params.logger,
  });
  const localBackendClient = createLocalBackendClient({
    getRuntimeBackendUrl: params.desktopRuntime.getBackendUrl,
    getLocalApiToken: params.getLocalApiToken,
    getRuntimeState: params.supabaseWiring.getRuntimeState,
  });
  const requestJson = localBackendClient.requestJson;
  const actionReadState = createActionReadState({ userDataDir: app.getPath('userData') });
  const historyFetch = createHistoryFetcher({
    requestJson,
    getRuntimeState: params.supabaseWiring.getRuntimeState,
    getCompletionUnreadSnapshot: actionReadState.snapshotCompletionUnread,
  });
  const historyDeleteItem = createHistoryItemDeleter({
    requestJson,
    closeConversationWindow: ({ kind, id }) => {
      const overlayId = params.notificationWindow.resolveOverlayId(
        kind === 'conversation' ? { actionId: id } : { suggestionId: id }
      );
      if (overlayId) params.notificationWindow.destroyOverlayWindow(overlayId);
    },
  });
  const overlayBootstrapFetch = createOverlayBootstrapFetcher({ requestJson });
  const getUserId = () => params.supabaseWiring.getLocalOwnerId();
  const actionApprovalDecisionFetch = createActionApprovalDecisionFetcher({
    requestJson,
    getUserId,
  });
  const actionApprovalModeFetch = createActionApprovalModeFetcher({ requestJson, getUserId });
  const actions = createActionFetcher({ requestJson, getUserId });
  const approvalPreferenceFetch = createApprovalPreferenceFetcher({ requestJson, getUserId });
  const workspaceSettingsFetch = createWorkspaceSettingsFetcher({ requestJson, getUserId });
  const selectWorkspaceFolder = async (): Promise<{ canceled: boolean; path: string | null }> => {
    // getAllWindows() lists the newest window first, which can be a hidden overlay;
    // a dialog parented to it would show that overlay.
    const mainWindow = params.getMainWindow();
    const options: Electron.OpenDialogOptions = { properties: ['openDirectory'] };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, options)
      : await dialog.showOpenDialog(options);
    return result.canceled || result.filePaths.length === 0
      ? { canceled: true, path: null }
      : { canceled: false, path: result.filePaths[0] };
  };
  const openExternalUrl = createExternalUrlOpener({
    isDevRuntime: params.isDevRuntime,
    webAppOrigin: params.desktopRuntime.config?.web_app_origin ?? null,
    logger: params.logger,
  });
  const capturePrivacy = createCapturePrivacyManager({
    userDataDir: app.getPath('userData'),
    initialUserId: null,
    resolveAppBundleId: resolveInstalledAppBundleId,
  });
  const screenshotSync = createScreenshotSyncManager({
    isMac: process.platform === 'darwin',
    userDataDir: app.getPath('userData'),
    getMainWindow: params.getMainWindow,
    isBackendRuntimeReady: () => params.supabaseWiring.getLocalOwnerId() !== null,
    getManifestPath: params.desktopRuntime.getAppRuntimeManifestPath,
    requestPermissions: requestCapturePermissions,
    readSource: (userId) =>
      requestJson({
        path: `/v1/agents/users/${encodeURIComponent(userId)}/context-source`,
        method: 'GET',
        timeoutMs: 10_000,
      }),
    transitionSource: (userId, body) =>
      requestJson({
        path: `/v1/agents/users/${encodeURIComponent(userId)}/context-source/transitions`,
        method: 'POST',
        body,
        timeoutMs: 10_000,
      }),
    containSource: async () => {
      const result = await params.localBackendAuthContextController.containRuntime();
      if (!result.contained) throw new Error(result.reason);
    },
    capturePrivacy,
    onCaptureStatusChanged: () => void params.updateUi.refreshCaptureStatus(),
    // The runtime greets only an owner who has no data yet, so every start may ask.
    onRecordingStarted: (userId) => {
      const { accelerator, failure } = shortcutController.getState();
      // A failure means the accelerator is configured but not registered: it would not work.
      const answer = getWelcomeSuggestionText(params.getUiLanguage(), failure ? null : accelerator);
      postWelcomeSuggestion({ requestJson, userId, answer }).catch((error: unknown) => {
        params.logger?.error?.('WELCOME_SUGGESTION_ERR', { err: error });
      });
    },
  });
  const rebuildMenus = () =>
    safely(() => {
      params.updateUi.rebuildTrayMenu();
      params.updateUi.rebuildAppMenu();
    }, undefined);
  /**
   * Recording that actually started proves macOS granted the permissions the gate
   * reads, so a conversation the gate deferred reaches its window here too: a start
   * made from the tray is preceded by no gate read, and the one the recording screen
   * makes is followed by the "later" that drops the request. A failure to open it
   * must not fail the start the user asked for.
   */
  const startScreenshots = async (): Promise<RecordingStartResult> => {
    const result = await screenshotSync.start();
    if (result === 'started') safely(conversationOverlay.openPendingRequest, undefined);
    return result;
  };

  const applyLocalOwner = (owner: LocalOwner): void => {
    const userId = owner.kind === 'account' ? owner.id : null;
    conversationOverlay.onOwnerChanged(owner.id);
    settingsScopeUserId = userId;
    const scopedCaptureSettings = capturePrivacy.setSettingsScope(userId);
    screenshotSync.setOwner(owner);
    const language = loadUiLanguage(params.resolveUiSettingsPath(userId), app.getLocale());
    params.setUiLanguage(language);
    broadcastUiLanguage(BrowserWindow.getAllWindows(), language);
    try {
      const win = params.getMainWindow();
      if (win && !win.isDestroyed()) {
        win.webContents.send('privacy:captureSettingsUpdated', scopedCaptureSettings);
      }
    } catch (error) {
      console.error('Failed to broadcast scoped capture settings:', error);
    }
    rebuildMenus();
    actionReadState.setSubject(owner.id);
  };

  const registerMainIpc = (): void => {
    startMainIpcRuntime({
      notificationWindow: params.notificationWindow,
      context: {
        ipcMain,
        aiConnection: params.aiConnection,
        getMainWindow: params.getMainWindow,
        openNewConversationOverlay: conversationOverlay.openNewConversationOverlay,
        openActionConversationOverlay: conversationOverlay.openActionConversationOverlay,
        getAllWindows: () => BrowserWindow.getAllWindows(),
        isDevRuntime: params.isDevRuntime,
        frontendDistIndex: params.frontendDistIndex,
        recordSecurityEvent: (name, metadata, level) =>
          params.logger?.event?.(name, metadata, level),
        saveLoginState: (isLoggedIn) => saveUserState(app.getPath('userData'), Boolean(isLoggedIn)),
        getSupabaseSessionManager: params.supabaseWiring.getSessionManager,
        getSupabaseSessionInitializationStatus: params.supabaseWiring.getInitializationStatus,
        getRuntimeState: params.supabaseWiring.getRuntimeState,
        signOut: params.supabaseWiring.signOut,
        startBrowserLogin: params.authCoordinator.startBrowserLogin,
        historyFetch,
        historyDeleteItem,
        markCompletionViewed: (request) => {
          actionReadState.markCompletionViewed(request);
          broadcastHistoryChanged(params.getMainWindow(), { source: 'read_state' });
        },
        actionFiles: {
          open: ({ path }) => shell.showItemInFolder(path),
        },
        update: {
          getReadyNotice: params.updateUi.getReadyNotice,
          restartToUpdate: params.updateUi.restartToUpdate,
        },
        actions: {
          ...actions,
          readConversationPage: (request) =>
            request.cursor === null && request.limit === ACTION_CONVERSATION_LATEST_PAGE_LIMIT
              ? latestActionPageReader.read(request.actionId)
              : actions.readConversationPage(request),
          getCurrentSubjectId: getUserId,
          resolveOverlayIdForSender: params.notificationWindow.resolveOverlayIdForSender,
          registerActionAssociation: params.notificationWindow.registerActionAssociation,
          refreshActionConversation: orchestration.refreshActionConversation,
        },
        actionImages: {
          localArtifactRoot: params.localArtifactRoot,
          decodeImageDimensions: (bytes) => {
            const decoded = nativeImage.createFromBuffer(bytes);
            if (decoded.isEmpty()) return null;
            const { width, height } = decoded.getSize();
            return { widthPx: width, heightPx: height };
          },
          revealInFolder: (absolutePath) => shell.showItemInFolder(absolutePath),
        },
        getUiSettingsPath: () => params.resolveUiSettingsPath(settingsScopeUserId),
        getUiLanguage: params.getUiLanguage,
        setUiLanguage: (language) => {
          params.setUiLanguage(language);
          rebuildMenus();
        },
        getWorkspaceEditCommandPreference: approvalPreferenceFetch.get,
        setWorkspaceEditCommandPreference: async (approvalMode) => {
          if (approvalMode !== 'prompt_each_time' && approvalMode !== 'always_allow') {
            throw new Error('Invalid approval mode.');
          }
          return await approvalPreferenceFetch.update(approvalMode as ApprovalMode);
        },
        getGlobalShortcutState: shortcutController.getState,
        setGlobalShortcutAccelerator: (accelerator) => {
          const result = shortcutController.changeShortcut(accelerator);
          // The tray menu displays the accelerator, so it must not keep showing the old keys.
          if (result.ok) rebuildMenus();
          return result;
        },
        workspaceSettingsGet: workspaceSettingsFetch.get,
        workspaceSettingsGetReadAccessScope: workspaceSettingsFetch.getReadAccessScope,
        workspaceSettingsGetCommandNetwork: workspaceSettingsFetch.getCommandNetwork,
        workspaceSettingsUpdateCommandNetwork: workspaceSettingsFetch.updateCommandNetwork,
        workspaceSettingsCreateOrganization: workspaceSettingsFetch.createOrganization,
        workspaceSettingsCreateProject: workspaceSettingsFetch.createProject,
        workspaceSettingsCreateFolder: workspaceSettingsFetch.createFolder,
        workspaceSettingsReorderProjects: workspaceSettingsFetch.reorderProjects,
        workspaceSettingsDeleteOrganization: workspaceSettingsFetch.deleteOrganization,
        workspaceSettingsDeleteProject: workspaceSettingsFetch.deleteProject,
        workspaceSettingsDeleteFolder: workspaceSettingsFetch.deleteFolder,
        workspaceSettingsUpdateProjectLinks: workspaceSettingsFetch.updateProjectLinks,
        workspaceSettingsUpdateFolderLinks: workspaceSettingsFetch.updateFolderLinks,
        workspaceSettingsUpdateReadAccessScope: workspaceSettingsFetch.updateReadAccessScope,
        workspaceSettingsSelectFolder: selectWorkspaceFolder,
        screenshotSync,
        startScreenshots,
        readCaptureGateState: conversationOverlay.readGateState,
        dismissRecordingIntro: conversationOverlay.dismissIntro,
        openCapturePermissionSettings,
        capturePrivacy,
        readAppIcon: readAppIconDataUrl,
        onCaptureSettingsChanged: () => void params.updateUi.refreshCaptureStatus(),
        openExternalUrl,
        wsSend: orchestration.sendFromRenderer,
        wsAcceptAction: orchestration.acceptAction,
        wsGetStatus: () => ({ status: orchestration.isConnected() ? 'connected' : 'disconnected' }),
        enqueueResumeRequest: (payload) => orchestration.enqueueResumeRequest(payload),
        resolveOverlayBootstrap,
        submitApprovalDecision: async (payload) => {
          const subjectId = getUserId();
          if (subjectId === null) throw new Error('Missing authenticated user id.');
          return await submitActionApprovalDecision(payload, {
            submitDecision: actionApprovalDecisionFetch,
            enqueueResumeRequest: orchestration.enqueueResumeRequest,
            isResultCurrent: () => getUserId() === subjectId,
            onDecisionSettled: (identity) => orchestration.handleApprovalDecisionSettled(identity),
          });
        },
        getActionApprovalMode: actionApprovalModeFetch.get,
        setActionApprovalMode: actionApprovalModeFetch.update,
        createNotificationIpcHandlers: params.notificationWindow.createNotificationIpcHandlers,
      },
    });
  };

  return {
    applyLocalOwner,
    prepareOwnerChange: () => {
      params.notificationWindow.clearForOwnerChange();
      conversationOverlay.onOwnerChanged(null);
      latestActionPageReader.clear();
      orchestration.resetActionLive();
      actionReadState.setSubject(null);
      return screenshotSync.pause('signed_out');
    },
    pauseCapture: screenshotSync.pause,
    restoreCaptureRecorder: async () => {
      try {
        await screenshotSync.restoreRecorder();
      } finally {
        // Conversation entry requires OS permission, not a running recorder.
        conversationOverlay.readGateState();
      }
    },
    disconnectOrchestration: orchestration.disconnect,
    ensureOrchestrationConnected: orchestration.ensureConnected,
    getCaptureStatusSnapshot: screenshotSync.getCaptureStatusSnapshot,
    getCurrentSubjectId: getUserId,
    getGlobalShortcutState: shortcutController.getState,
    initializeGlobalShortcut: shortcutController.initialize,
    openNewConversationOverlay: conversationOverlay.openNewConversationOverlay,
    reconnectOrchestration: orchestration.reconnect,
    registerMainIpc,
    startScreenshots,
    stopScreenshots: screenshotSync.stop,
  };
}
