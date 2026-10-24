const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory');

// Plain Node runs this, so Electron is a stand-in. The module is loaded once; each runtime
// points it at its own userData.
const current = { dir: null, context: null };
const fakeElectron = {
  app: { getPath: () => current.dir, getLocale: () => 'en' }, BrowserWindow: { getAllWindows: () => [] },
  systemPreferences: {}, dialog: {}, globalShortcut: {}, ipcMain: {}, nativeImage: {}, shell: {},
};
const load = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === 'electron') return fakeElectron;
  if (request === './ipcBootstrap') return { startMainIpcRuntime: options => {
    current.context = buildMainContext(options.context);
    return { context: current.context };
  } };
  return load.call(this, request, parent, isMain);
};
let createDesktopFeatureRuntime;
try {
  ({ createDesktopFeatureRuntime } = require('../electron/dist/main_runtime/featureRuntime'));
} finally { Module._load = load; }

// The desktop runtime with what a Suggestion panel needs: the socket's auto-open entry and the
// setup state main saves.
function startRuntime(t, onboardingFile) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-panel-hold-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  if (onboardingFile) fs.writeFileSync(path.join(dir, 'onboarding.json'), onboardingFile);
  current.dir = dir;

  const opened = [];
  let autoOpen;
  const owner = { id: 'guest-id', kind: 'guest' };
  const feature = createDesktopFeatureRuntime({
    aiConnection: {}, frontendDistIndex: '/tmp/index.html', localArtifactRoot: dir, isDevRuntime: () => false,
    desktopRuntime: { config: null, getBackendUrl: () => 'http://127.0.0.1:61200' },
    notificationWindow: {
      setLocalOwnerIdGetter: () => {}, clearForOwnerChange: () => {}, resolveOverlayId: () => null,
      hasOverlayWindow: () => false, setOverlayPlacementGetter: () => {}, setActionLiveSnapshotGetter: () => {},
      clearActionAssociations: () => {}, sendResetToAllOverlays: () => {}, showNotification: id => opened.push(id),
    },
    createOrchestrationWS: options => ({ connect: () => { autoOpen = options.showNotification; } }),
    authCoordinator: {}, localBackendAuthContextController: {},
    supabaseWiring: {
      getLocalOwnerId: () => owner.id,
      getRuntimeState: () => ({ status: 'ready', owner, message: null }),
      getSessionManager: () => null, getInitializationStatus: () => 'ready', signOut: async () => {},
    },
    getLocalApiToken: () => 'local-owner-token',
    updateUi: { rebuildTrayMenu: () => {}, rebuildAppMenu: () => {}, refreshCaptureStatus: async () => {} },
    getMainWindow: () => null, createMainWindow: () => {}, resolveUiSettingsPath: () => path.join(dir, 'ui.json'),
    getUiLanguage: () => 'en', setUiLanguage: () => {}, logger: null,
  });
  feature.registerMainIpc();
  feature.ensureOrchestrationConnected();
  return { feature, context: current.context, opened, arrive: id => autoOpen(id) };
}

test('Suggestion panels wait for setup and open once, in arrival order, for the owner they came for', async t => {
  const { feature, context, opened, arrive } = startRuntime(t, null);
  arrive('previous-owner');
  await feature.prepareOwnerChange();
  arrive('welcome');
  arrive('second');
  context.onboarding.setState({ status: 'in_progress', step: 'web_search' });
  assert.deepEqual(opened, []);

  context.onboarding.setState({ status: 'completed' });
  context.onboarding.setState({ status: 'completed' });
  arrive('later');
  assert.deepEqual(opened, ['welcome', 'second', 'later']);
});

test('a user who finished setup sees panels as they arrive', t => {
  const { opened, arrive } = startRuntime(t, '{"status":"completed"}\n');
  arrive('suggestion');
  assert.deepEqual(opened, ['suggestion']);
});
