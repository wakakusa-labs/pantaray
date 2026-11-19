const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory');
const {
  resolveScopedSettingsPath,
  initializeAccountSettingsScope,
} = require('../electron/dist/settings/scope');
const { saveUiLanguage } = require('../electron/dist/ui/uiLanguage');
const { fakeBrowserElectron } = require('./helpers/browser_electron_fakes');

test('desktop features bind guest and expired-account requests, preferences, read state, and sockets to the confirmed owner', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-owner-features-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const previousPermissions = process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS;
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '1';
  t.after(() => {
    if (previousPermissions === undefined)
      delete process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS;
    else process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = previousPermissions;
  });
  // Electron adds `process.getSystemVersion`; plain Node, which runs these tests, has none.
  const previousGetSystemVersion = process.getSystemVersion;
  process.getSystemVersion = () => '15.0.0';
  t.after(() => {
    process.getSystemVersion = previousGetSystemVersion;
  });
  const windows = new Set();
  let context;
  // Browser pages that never finish loading, so one is in flight when the owner changes.
  const browser = fakeBrowserElectron({ load: () => new Promise(() => {}) });
  const fakeElectron = {
    app: { getPath: () => dir, getLocale: () => 'en' },
    BrowserWindow: class extends browser.electron.BrowserWindow {
      static getAllWindows() {
        return [];
      }
    },
    session: browser.electron.session,
    systemPreferences: {
      isTrustedAccessibilityClient: () => false,
      getMediaAccessStatus: () => 'denied',
    },
    dialog: {},
    globalShortcut: {},
    ipcMain: {},
    nativeImage: {},
    shell: {},
  };
  const load = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === 'electron') return fakeElectron;
    if (request === './ipcBootstrap')
      return {
        startMainIpcRuntime: (options) => {
          context = buildMainContext(options.context);
          return { context };
        },
      };
    return load.call(this, request, parent, isMain);
  };
  let createDesktopFeatureRuntime;
  try {
    ({ createDesktopFeatureRuntime } = require('../electron/dist/main_runtime/featureRuntime'));
  } finally {
    Module._load = load;
  }

  let owner = null;
  let language = 'en';
  let receiveBrowser = null;
  const browserOperation = (id) => ({
    event: 'browser_operation_requested',
    data: {
      operation_request_id: id,
      session_id: 'owner-session',
      operation: { action: 'navigate', target: { kind: 'web', url: 'https://a.example/' } },
      policy: {
        allow_network: true,
        file_roots: [],
        private_roots: [],
        private_root_exceptions: [],
        secret_paths: [],
        write_hosts: [],
      },
      user_browser: null,
    },
  });
  const requests = [];
  const sockets = [];
  let overlayOwnerId;
  const browserAnswers = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.headers.Authorization, 'Bearer local-owner-token');
    if (new URL(url).pathname === '/local/action-browser') {
      browserAnswers.push(JSON.parse(options.body).result);
      return new Response('{}');
    }
    requests.push(new URL(url).pathname);
    return new Response(JSON.stringify({ command_network_enabled: false }));
  });
  const uiPath = (userId) =>
    resolveScopedSettingsPath({ userDataDir: dir, userId, fileName: 'ui-settings.json' });
  saveUiLanguage(uiPath(null), 'ja');
  const feature = createDesktopFeatureRuntime({
    aiConnection: {},
    frontendDistIndex: '/tmp/index.html',
    localArtifactRoot: dir,
    isDevRuntime: () => false,
    desktopRuntime: { config: null, getBackendUrl: () => 'http://127.0.0.1:61200' },
    notificationWindow: {
      setLocalOwnerIdGetter: (getter) => {
        overlayOwnerId = getter;
      },
      clearForOwnerChange: () => windows.clear(),
      resolveOverlayId: () => null,
      hasOverlayWindow: (id) => windows.has(id),
      openStandaloneConversationOverlay: (id) => {
        windows.add(id);
        return 'created';
      },
      destroyOverlayWindow: (id) => windows.delete(id),
      setActionLiveSnapshotGetter: () => {},
      sendResetToAllOverlays: () => {},
      clearActionAssociations: () => {},
      setOverlayPlacementGetter: () => {},
    },
    createOrchestrationWS: (hooks) => {
      receiveBrowser = hooks.forwardEventToRenderers;
      return {
        connect: (url, headers) => sockets.push({ url, headers }),
        disconnect: () => {},
      };
    },
    authCoordinator: { startBrowserLogin: async () => ({ ok: true }) },
    localBackendAuthContextController: {},
    supabaseWiring: {
      getLocalOwnerId: () => owner?.id ?? null,
      getRuntimeState: () => ({ status: owner ? 'ready' : 'syncing', owner, message: null }),
      getSessionManager: () => ({
        getState: () => ({
          authStatus: owner?.kind === 'account' ? 'expired' : 'unauthenticated',
          isLoggedIn: false,
          user: null,
        }),
      }),
      getInitializationStatus: () => 'ready',
      signOut: async () => {},
    },
    getLocalApiToken: () => 'local-owner-token',
    updateUi: {
      rebuildTrayMenu: () => {},
      rebuildAppMenu: () => {},
      refreshCaptureStatus: async () => {},
    },
    getMainWindow: () => null,
    createMainWindow: () => {},
    resolveUiSettingsPath: uiPath,
    getUiLanguage: () => language,
    setUiLanguage: (next) => {
      language = next;
    },
    logger: null,
  });
  feature.registerMainIpc();
  await assert.rejects(
    context.workspaceSettings.getCommandNetwork(),
    /Missing authenticated user id/
  );
  assert.equal(requests.length, 0);

  const bind = async (next) => {
    owner = null;
    await feature.prepareOwnerChange();
    if (next.kind === 'account')
      initializeAccountSettingsScope({ userDataDir: dir, accountUserId: next.id });
    feature.applyLocalOwner(next);
    owner = next;
    feature.reconnectOrchestration();
  };
  const mark = (subjectId) =>
    context.history.markCompletionViewed({
      subjectId,
      actionId: 'action-1',
      completionEventId: `completion-${subjectId}`,
    });
  const readState = (id) => path.join(dir, 'settings', id, 'action-read-state.json');
  for (const next of [
    { id: 'guest-id', kind: 'guest' },
    { id: 'account-a', kind: 'account' },
  ]) {
    await bind(next);
    assert.equal(context.auth.getState().user, null);
    assert.equal(context.actions.getCurrentSubjectId(), next.id);
    assert.equal(overlayOwnerId(), next.id);
    assert.equal(language, 'ja');
    await context.workspaceSettings.getCommandNetwork();
    assert.deepEqual(requests.splice(0), [
      `/v1/agents/users/${next.id}/workspace-settings/command-network`,
    ]);
    assert.equal(
      sockets.at(-1).url,
      `ws://127.0.0.1:61200/v1/agents/users/${next.id}/orchestrations`
    );
    assert.equal(sockets.at(-1).headers.Authorization, 'Bearer local-owner-token');
    assert.equal(fs.existsSync(readState(next.id)), false);
    mark(next.id);
    assert.equal(
      JSON.parse(fs.readFileSync(readState(next.id), 'utf8')).entries[0].completion_event_id,
      `completion-${next.id}`
    );
  }
  assert.throws(() => mark('guest-id'), /subject does not match/);
  saveUiLanguage(uiPath(null), 'en');
  await bind({ id: 'guest-id', kind: 'guest' });
  assert.equal(language, 'en');
  assert.equal(
    JSON.parse(fs.readFileSync(readState('guest-id'), 'utf8')).entries[0].completion_event_id,
    'completion-guest-id'
  );
  await bind({ id: 'account-a', kind: 'account' });
  // A browser page still loading, and an artifact the backend has not taken yet.
  receiveBrowser(browserOperation('op-in-flight'));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(browser.windows.length, 1);
  const leftover = path.join(dir, '.tmp', 'browser', 'op-earlier.png');
  fs.mkdirSync(path.dirname(leftover), { recursive: true });
  fs.writeFileSync(leftover, 'png');
  assert.equal(language, 'ja');
  assert.equal(
    JSON.parse(fs.readFileSync(readState('account-a'), 'utf8')).entries[0].completion_event_id,
    'completion-account-a'
  );
  // Close old content and revoke read receipts synchronously, while the helper
  // may still be held in its old owner or fail to configure the next one.
  assert.equal(feature.openNewConversationOverlay(), 'created');
  assert.equal(windows.size, 1);
  windows.add('native-history-window');
  owner = null;
  const prepared = feature.prepareOwnerChange();
  assert.equal(windows.size, 0);
  // No close for the old owner's session will come: it ends with the switch. The backend that
  // may still read the artifact keeps running, so it stays.
  assert.equal(browser.windows[0].destroyed, true);
  assert.ok(fs.existsSync(leftover));
  assert.equal(feature.openNewConversationOverlay(), 'initializing');
  assert.throws(() => mark('account-a'));
  assert.throws(() => context.privacy.getCaptureSettings(), /Local owner is unavailable/);
  assert.throws(() => context.ui.setLanguage('en'), /Local owner is unavailable/);
  await prepared;
  assert.equal(language, 'ja');
  assert.equal(JSON.parse(fs.readFileSync(uiPath('account-a'), 'utf8')).ui_language, 'ja');
  await bind({ id: 'account-a', kind: 'account' });
  receiveBrowser(browserOperation('op-after'));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(browserAnswers.at(-1).code, 'BROWSER_SESSION_GONE');
  assert.equal(browser.windows.length, 1);
  // The backend generation ends: no runtime can take the artifact any more.
  feature.closeBrowserSessions();
  assert.equal(fs.existsSync(leftover), false);
  assert.equal(feature.openNewConversationOverlay(), 'created');
  // The ready broadcast must replay a deferred window even without another
  // renderer focus or recording-start event after a token refresh.
  const existingWindows = windows.size;
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '0';
  assert.equal(feature.openNewConversationOverlay(), 'recording_intro');
  owner = null;
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '1';
  assert.equal(context.screenshot.getGateState().introRequested, true);
  assert.equal(windows.size, existingWindows);
  owner = { id: 'account-a', kind: 'account' };
  await feature.restoreCaptureRecorder();
  assert.equal(windows.size, existingWindows + 1);
  await feature.restoreCaptureRecorder();
  assert.equal(windows.size, existingWindows + 1);
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '0';
  assert.equal(feature.openNewConversationOverlay(), 'recording_intro');
  owner = null;
  process.env.PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS = '1';
  assert.equal(context.screenshot.getGateState().introRequested, true);
  owner = { id: 'account-a', kind: 'account' };
  fs.writeFileSync(
    resolveScopedSettingsPath({
      userDataDir: dir,
      userId: owner.id,
      fileName: 'screenshot-settings.json',
    }),
    '{invalid-json'
  );
  await assert.rejects(feature.restoreCaptureRecorder(), SyntaxError);
  assert.equal(windows.size, existingWindows + 2);
  feature.disconnectOrchestration();
});
