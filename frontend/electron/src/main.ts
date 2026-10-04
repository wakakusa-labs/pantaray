import { BrowserWindow, Menu, app, dialog, protocol, safeStorage, screen, shell } from 'electron';
import path from 'path';
import { createRequire } from 'module';

import type { UiLanguage } from './ipc/context';
import { loadUiLanguage } from './ui/uiLanguage';
import { getStartupDialogCopy } from './ui/mainProcessCopy';
import {
  initializeAccountSettingsScope,
  resolveScopedSettingsPath,
  SCOPED_PREFERENCE_FILES,
} from './settings/scope';
import { createAuthCoordinator } from './auth/authCoordinator';
import { broadcastAuthStateToAllWindows } from './auth/authBroadcast';
import { APP_PROTOCOL, createDeepLinkAuthManager } from './auth/deepLinkAuth';
import { extractUserIdFromJwt } from './auth/jwt';
import { createLocalConnectionRuntime } from './aiConnection/localConnectionRuntime';
import { createLocalBackendHelperManager } from './auth/localBackendHelperManager';
import { createLocalBackendSessionSync } from './auth/localBackendSessionSync';
import { createLoopbackAuthTransport, type LoopbackAuthTransport } from './auth/loopbackAuth';
import { type SupabaseSessionManagerLike } from './auth/contracts';
import { createSupabaseWiring } from './auth/supabaseWiring';
import { PANTARAY_ACCOUNT_LOGIN_ENABLED } from './auth/accountLoginFeature';
import { saveUserState } from './auth/userState';
import { createOverlayAwareActivateHandler } from './windows/overlayActivation';
import { ensureMainWindowFocused } from './windows/windowVisibility';
import { hasUpdateFeed } from './update/desktopUpdater';
import type { DesktopUpdater } from './update/desktopUpdater';
import { createDesktopUpdaterForMain } from './update/desktopUpdaterBootstrap';
import type { NotificationWindowApi } from './orchestration/contracts';
import type { CreateOrchestrationWS } from './orchestration/orchestrationManager';
import { reportFatalStartupFailure } from './main_runtime/startupFailure';
import { createUpdateUiManager } from './main_runtime/updateUi';
import { createTrayController } from './main_runtime/trayController';
import { installDesktopApplicationLifecycle } from './main_runtime/applicationLifecycle';
import { openNewWindowsInDefaultBrowser } from './security/windowOpenPolicy';
import { startDesktopBackgroundRuntime } from './main_runtime/backgroundStartup';
import { createDesktopRuntime, loadDevelopmentEnvironment } from './main_runtime/desktopRuntime';
import { createDesktopFeatureRuntime } from './main_runtime/featureRuntime';
import { applyDevUserDataDirOverride } from './runtime/userDataOverride';
import { registerActionImageProtocol, registerActionImageScheme } from './protocol/imageProtocol';

type LoggerLike = {
  isPackaged?: boolean;
  event?: (name: string, payload: unknown, level?: string) => void;
  info?: (name: string, payload?: unknown) => void;
  warn?: (name: string, payload?: unknown) => void;
  error?: (name: string, payload?: unknown) => void;
  debug?: (name: string, payload?: unknown) => void;
  safeUrlSummary?: (url: string) => unknown;
  fingerprint?: (value: unknown) => string;
};

type ConsoleMethodName = 'log' | 'info' | 'warn' | 'error' | 'debug';
type MutableConsole = Console & Record<ConsoleMethodName, (...args: unknown[]) => void>;
type AppWithQuitFlag = Electron.App & { isQuitting?: boolean };
type ProcessWithDefaultApp = NodeJS.Process & { defaultApp?: boolean };
type GlobalWithOptionalWebSocket = typeof globalThis & {
  WebSocket?: typeof WebSocket;
};
const loadNodeModule = createRequire(__filename);

// dist/main.js から見たルート解決
const FRONTEND_ROOT = path.resolve(__dirname, '../..'); // .../frontend
const FRONTEND_DIST_INDEX = path.join(FRONTEND_ROOT, 'dist', 'index.html');

// ---- Logging bootstrap (must run early) ----
process.env.PANTARAY_PACKAGED = app.isPackaged ? '1' : '0';

function normalizeNodeEnv(value: string | undefined): 'development' | 'production' | 'test' {
  const normalized = String(value || '')
    .trim()
    .toLowerCase();
  if (normalized === 'production' || normalized === 'prod') return 'production';
  if (normalized === 'test') return 'test';
  return 'development';
}
process.env.NODE_ENV = app.isPackaged ? 'production' : normalizeNodeEnv(process.env.NODE_ENV);
applyDevUserDataDirOverride({ app });
// Chromium freezes the scheme registry when the app becomes ready, so `pantaray-image://`
// has to be declared here rather than alongside its handler.
registerActionImageScheme(protocol);

const { createLogger, disableConsoleForRelease } = loadNodeModule('../logger') as {
  createLogger: () => LoggerLike;
  disableConsoleForRelease: () => void;
};
disableConsoleForRelease();

const { configureFileSink, FILE_SINK_LEVEL } = loadNodeModule('../log_file_sink') as {
  configureFileSink: (config: { dir: string; level: string }) => void;
  FILE_SINK_LEVEL: string;
};

let logger: LoggerLike | null = null;

function installSafeConsole(log: LoggerLike | null): void {
  if (!log || log.isPackaged) return;
  const map: Record<string, string> = {
    log: 'info',
    info: 'info',
    warn: 'warn',
    error: 'error',
    debug: 'debug',
  };
  for (const [method, level] of Object.entries(map)) {
    try {
      (console as MutableConsole)[method as ConsoleMethodName] = (...args: unknown[]) => {
        try {
          log.event?.('CONSOLE', { method, args }, level);
        } catch {
          // no-op
        }
      };
    } catch {
      // no-op
    }
  }
}

logger = createLogger();
installSafeConsole(logger);

// Configure the error-only file sink as early as possible — before the runtime
// config load and the whenReady block — so release-time startup failures (e.g.
// RUNTIME_CONFIG_ERR) are recorded. app.getPath('logs') is valid before 'ready'.
configureFileSink({ dir: app.getPath('logs'), level: FILE_SINK_LEVEL });

function isDevRuntime(): boolean {
  return process.env.NODE_ENV === 'development' || !app.isPackaged;
}

const LOCAL_BACKEND_ARTIFACT_ROOT_DIRNAME = 'local-backend-artifacts';
const LOCAL_BACKEND_ARTIFACT_ROOT = path.join(
  app.getPath('userData'),
  LOCAL_BACKEND_ARTIFACT_ROOT_DIRNAME
);
let desktopUpdater: DesktopUpdater | null = null;
let pendingManualUpdateCheck = false;

function canCheckForUpdatesNow(): boolean {
  // 更新の取得は公開 GitHub Releases なのでログイン状態に依らない。
  return hasUpdateFeed();
}

loadDevelopmentEnvironment({ frontendRoot: FRONTEND_ROOT, isDevelopment: isDevRuntime() });

// ---- Log salt (packaged builds do not trust .env) ----
// 最初のログ emit より前に salt を必ず用意する。未設定だと redact() の fingerprint() が
// throw し、起動失敗ログ（RUNTIME_CONFIG_ERR / APP_STARTUP_ERR）が詳細を失うため。
// dev は .env の値、配布版は userData に永続化したマシン固有 salt を使う。
const { ensureLogSalt } = loadNodeModule('../log_salt') as {
  ensureLogSalt: (userDataDir: string) => string;
};
process.env.PANTARAY_LOG_SALT = ensureLogSalt(app.getPath('userData'));

const desktopRuntime = createDesktopRuntime({ app, dialog, frontendRoot: FRONTEND_ROOT, logger });

// ---- WebSocket polyfill for Supabase Realtime ----
// eslint-disable-next-line @typescript-eslint/no-require-imports
const NodeWebSocket = require('ws') as typeof WebSocket;
try {
  const globalWithWebSocket = globalThis as GlobalWithOptionalWebSocket;
  if (typeof globalWithWebSocket.WebSocket === 'undefined') {
    globalWithWebSocket.WebSocket = NodeWebSocket;
  }
} catch {
  // no-op
}

// ---- External modules (CommonJS) ----
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { SupabaseSessionManager } = require('../supabase_session_manager') as {
  SupabaseSessionManager: new (opts: unknown) => SupabaseSessionManagerLike;
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { AuthAttemptStore } = require('../auth_attempt_store') as {
  AuthAttemptStore: new (opts: unknown) => {
    put?: (attemptId: string, verifier: string) => void;
    get?: (attemptId: string) => string | null;
    consume?: (attemptId: string) => string | null;
  };
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const notificationWindow = require('../notification_window') as NotificationWindowApi;
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { createOrchestrationWS } = require('../ws_orchestration') as {
  createOrchestrationWS: CreateOrchestrationWS;
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { createMainWindow } = require('../window_lifecycle') as {
  createMainWindow: (options: { initialUiLanguage: UiLanguage }) => BrowserWindow;
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { createClient } = require('@supabase/supabase-js') as {
  createClient: (url: string, key: string, options?: unknown) => unknown;
};

// ---- Main state (SSOT) ----
let mainWindow: BrowserWindow | null = null;

const overlayAwareActivateHandler = createOverlayAwareActivateHandler({
  overlay: notificationWindow,
  screen,
});

// ---- UI language (SSOT) ----
function resolveUiSettingsPath(userId: string | null): string {
  return resolveScopedSettingsPath({
    userDataDir: app.getPath('userData'),
    userId,
    fileName: SCOPED_PREFERENCE_FILES.ui,
  });
}
let uiLanguage: UiLanguage = loadUiLanguage(resolveUiSettingsPath(null), app.getLocale());
notificationWindow.setUiLanguageGetter?.(() => uiLanguage);

// ---- Browser auth callback transports ----
const authCoordinatorRef: { current: ReturnType<typeof createAuthCoordinator> | null } = {
  current: null,
};

const loopbackAuth: LoopbackAuthTransport = createLoopbackAuthTransport({
  // ポータル側の戻り先（frontend/src/lib/desktopAuthReturn.ts）と揃える。
  port: 32100,
  ttlMs: 2 * 60 * 1000,
  callbackPath: '/auth/callback',
  getRuntimeConfig: () => desktopRuntime.config,
  handleCallback: (payload) => {
    const coordinator = authCoordinatorRef.current;
    if (!coordinator)
      return { ok: false, statusCode: 409, message: 'Auth coordinator is not ready.' };
    return coordinator.handleCallback(payload);
  },
  focusMainWindow: () => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        if (!mainWindow.isVisible()) mainWindow.show();
        mainWindow.focus();
      }
    } catch {
      // no-op
    }
  },
  onTimeout: () => authCoordinatorRef.current?.expireCurrentAttempt(),
  logger,
});

const authCoordinator = createAuthCoordinator({
  accountLoginEnabled: PANTARAY_ACCOUNT_LOGIN_ENABLED,
  AuthAttemptStore,
  userDataDir: app.getPath('userData'),
  safeStorage,
  ttlMs: 2 * 60 * 1000,
  getRuntimeConfig: () => desktopRuntime.config,
  getSupabaseSessionManager: () => supabaseWiring.getSessionManager(),
  getLoopbackTransport: () => loopbackAuth,
  focusMainWindow: () => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        if (!mainWindow.isVisible()) mainWindow.show();
        mainWindow.focus();
      }
    } catch {
      // no-op
    }
  },
  logger,
});
authCoordinatorRef.current = authCoordinator;

const localBackendSessionSync = createLocalBackendSessionSync({
  getControlSocketPath: desktopRuntime.getLocalBackendControlSocketPath,
  logger,
});

const localBackendHelperManager = createLocalBackendHelperManager({
  isDevRuntime: isDevRuntime(),
  agentsRoot: path.resolve(FRONTEND_ROOT, '..', 'agents'),
  resourcesPath: process.resourcesPath,
  getControlSocketPath: desktopRuntime.getLocalBackendControlSocketPath,
  getHelperExecutablePath: desktopRuntime.getLocalBackendHelperExecutablePath,
  getLoopbackBinding: desktopRuntime.getLoopbackBinding,
  onReady: ({ runtimeBackendUrl: readyRuntimeBackendUrl }) => {
    desktopRuntime.setBackendUrl(readyRuntimeBackendUrl);
    featureRuntime.ensureOrchestrationConnected();
  },
  onUnexpectedExit: () => supabaseWiring.reportRuntimeUnavailable(),
  logger,
});

const localConnectionRuntime = createLocalConnectionRuntime({
  userDataDir: app.getPath('userData'),
  safeStorage,
  whenReady: () => app.whenReady(),
  onChanged: () => {
    if (mainWindow && !mainWindow.isDestroyed())
      mainWindow.webContents.send('aiConnection:changed');
  },
  openExternal: (url) => shell.openExternal(url),
  sessionSync: localBackendSessionSync,
  helperManager: localBackendHelperManager,
  onRuntimeUnavailable: () => supabaseWiring.reportRuntimeUnavailable(),
  logger,
});

const localBackendAuthContextController = localConnectionRuntime.authContext;

// ---- Supabase session (wiring module) ----
const supabaseWiring = createSupabaseWiring({
  accountLoginEnabled: PANTARAY_ACCOUNT_LOGIN_ENABLED,
  clearForSignOut: localBackendAuthContextController.clearForSignOut,
  getLocalConnectionStatus: localBackendSessionSync.getConnectionStatus,
  createClient,
  SupabaseSessionManager,
  runtimeConfig: desktopRuntime.config,
  safeStorage,
  userDataDir: app.getPath('userData'),
  extractUserIdFromJwt,
  beforeOwnerChanged: () => featureRuntime.prepareOwnerChange(),
  onAuthStateBroadcast: (state) => {
    broadcastAuthStateToAllWindows(BrowserWindow.getAllWindows(), state);
    if (state.runtimeState.status === 'ready' && state.runtimeState.owner) {
      void featureRuntime.restoreCaptureRecorder().catch((error) => {
        logger?.error?.('ZANEI_START_ERR', { error });
      });
    }
  },
  onAuthTokenChanged: () => {
    try {
      updateUi.rebuildTrayMenu();
      updateUi.rebuildAppMenu();
    } catch {
      // no-op
    }
  },
  onLoginStatePersist: (isLoggedIn) => saveUserState(app.getPath('userData'), Boolean(isLoggedIn)),
  onLocalOwnerChanged: (owner) => {
    if (owner.kind === 'account') {
      initializeAccountSettingsScope({
        userDataDir: app.getPath('userData'),
        accountUserId: owner.id,
      });
    }
    featureRuntime.applyLocalOwner(owner);
  },
  onAuthContextChanged: async ({ token, userId, sessionVersion, expiredIdentity }) => {
    await localBackendAuthContextController.applyAuthContextChange({
      token,
      userId,
      sessionVersion,
      expiredIdentity,
    });
  },
  onOrchestrationDisconnect: () => {
    try {
      featureRuntime.disconnectOrchestration();
    } catch {
      // no-op
    }
  },
  onOrchestrationReconnect: () => {
    try {
      featureRuntime.reconnectOrchestration();
    } catch {
      // no-op
    }
  },
});

const trayController = createTrayController({
  app,
  frontendRoot: FRONTEND_ROOT,
  getMainWindow: () => mainWindow,
  logger,
  rebuildAppMenu: () => updateUi.rebuildAppMenu(),
  rebuildTrayMenu: () => updateUi.rebuildTrayMenu(),
  refreshCaptureStatus: () => updateUi.refreshCaptureStatus(),
});
const updateUi = createUpdateUiManager({
  app,
  dialog,
  menu: Menu,
  getUiLanguage: () => uiLanguage,
  getDesktopUpdater: () => desktopUpdater,
  canCheckForUpdatesNow,
  markManualUpdateCheckPending: () => {
    pendingManualUpdateCheck = true;
  },
  consumeManualUpdateCheckPending: () => {
    const wasPending = pendingManualUpdateCheck;
    pendingManualUpdateCheck = false;
    return wasPending;
  },
  getTray: () => trayController.getTray(),
  getMainWindow: () => mainWindow,
  openNewConversationOverlay: () => {
    try {
      featureRuntime.openNewConversationOverlay();
    } catch (error) {
      logger?.error?.('NEW_CONVERSATION_TRAY_ERR', { err: error });
    }
  },
  getGlobalShortcutAccelerator: () => {
    const { accelerator, failure } = featureRuntime.getGlobalShortcutState();
    // A failure means the accelerator is configured but not registered: it would not work.
    return failure ? null : accelerator;
  },
  getCaptureStatusSnapshot: () => featureRuntime.getCaptureStatusSnapshot(),
  startScreenshots: () => featureRuntime.startScreenshots(),
  stopScreenshots: () => featureRuntime.stopScreenshots(),
  setTrayStatusVisual: (visual) => trayController.setStatusVisual(visual),
  onQuitRequested: () => {
    (app as AppWithQuitFlag).isQuitting = true;
  },
});

function initializeDesktopUpdater(): void {
  if (desktopUpdater) return;
  desktopUpdater = createDesktopUpdaterForMain({
    getUiLanguage: () => uiLanguage,
    updateUi,
    dialog,
    logger,
  });
  desktopUpdater.start();
  if (updateUi.consumeManualUpdateCheckPending()) {
    updateUi.handleCheckUpdatesClick(updateUi.getUpdateMenuCopy(uiLanguage));
  }
}

const featureRuntime = createDesktopFeatureRuntime({
  aiConnection: localConnectionRuntime,
  frontendDistIndex: FRONTEND_DIST_INDEX,
  localArtifactRoot: LOCAL_BACKEND_ARTIFACT_ROOT,
  isDevRuntime,
  desktopRuntime,
  notificationWindow,
  createOrchestrationWS,
  authCoordinator,
  localBackendAuthContextController,
  supabaseWiring,
  getLocalApiToken: localBackendHelperManager.getLocalApiToken,
  updateUi,
  getMainWindow: () => mainWindow,
  createMainWindow: createWindow,
  resolveUiSettingsPath,
  getUiLanguage: () => uiLanguage,
  setUiLanguage: (language) => (uiLanguage = language),
  logger,
});

// ---- Deep link auth (TS module) ----
const deepLinkAuth = createDeepLinkAuthManager({
  app,
  protocol: APP_PROTOCOL,
  getMainWindow: () => mainWindow,
  handleCallback: (payload) => authCoordinator.handleCallback(payload),
});
deepLinkAuth.installAppHandlers();

function createWindow(): void {
  mainWindow = createMainWindow({ initialUiLanguage: uiLanguage });
  deepLinkAuth.onMainWindowCreated();
}

function startBackgroundRuntime(): void {
  void startDesktopBackgroundRuntime({
    installLocalBackendConfig: desktopRuntime.installLocalBackendConfig,
    ensureLocalBackendStarted: () => localBackendHelperManager.ensureStarted(),
    initializeAuth: () => supabaseWiring.initialize(),
    applyPendingAuthCallback: () => {
      void authCoordinator.applyPendingCallback();
    },
    ensureOrchestrationConnected: featureRuntime.ensureOrchestrationConnected,
    reportRuntimeConfigFailure: (error) => {
      reportFatalStartupFailure({
        app,
        dialog,
        error,
        getUiLanguage: () => uiLanguage,
        kind: 'runtime-config',
        logger,
        quitPackagedApp: true,
        stage: 'local-backend-runtime-config',
      });
    },
    logger,
  }).catch((error) => {
    logger?.error?.('BACKGROUND_RUNTIME_START_ERR', { err: error });
  });
}

openNewWindowsInDefaultBrowser({ app, openExternal: (url) => shell.openExternal(url), logger });

installDesktopApplicationLifecycle({
  app,
  protocol: APP_PROTOCOL,
  argv: process.argv,
  execPath: process.execPath,
  platform: process.platform,
  isDefaultApp: Boolean((process as ProcessWithDefaultApp).defaultApp),
  initializeTray: () => trayController.initialize(),
  initializeUpdater: initializeDesktopUpdater,
  rebuildAppMenu: () => updateUi.rebuildAppMenu(),
  registerIpc: () => {
    featureRuntime.registerMainIpc();
    // Registered with IPC so the renderer can never reach an image URL before the handler that
    // authorizes it exists.
    registerActionImageProtocol(protocol, {
      localArtifactRoot: LOCAL_BACKEND_ARTIFACT_ROOT,
      getCurrentSubjectId: featureRuntime.getCurrentSubjectId,
    });
  },
  createMainWindow: createWindow,
  initializeGlobalShortcut: featureRuntime.initializeGlobalShortcut,
  handleStartupArgs: (argv) => deepLinkAuth.handleStartupArgs(argv),
  startBackgroundRuntime,
  activate: () => {
    void updateUi.refreshCaptureStatus();
    overlayAwareActivateHandler.handleActivate(() =>
      ensureMainWindowFocused({ getMainWindow: () => mainWindow, createMainWindow: createWindow })
    );
  },
  shutdown: async () => {
    localConnectionRuntime.dispose();
    try {
      await featureRuntime.pauseCapture('shutdown');
    } finally {
      await localBackendHelperManager.stopForShutdown();
    }
  },
  showMainWindowCreateError: (error) => {
    dialog.showErrorBox(
      '起動エラー',
      `メインウィンドウの作成に失敗しました: ${
        error instanceof Error ? error.message : String(error)
      }`
    );
  },
  showStartupError: (error) => {
    const copy = getStartupDialogCopy(uiLanguage);
    dialog.showErrorBox(
      copy.startupTitle,
      copy.startupBody(error instanceof Error ? error.message : String(error))
    );
  },
  logger,
});
