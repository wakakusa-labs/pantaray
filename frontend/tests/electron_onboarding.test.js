const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const { createOnboardingStateStore } = require('../electron/dist/settings/onboardingState');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-onboarding-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function createStore(dir) {
  const reports = [];
  return {
    store: createOnboardingStateStore({
      userDataDir: dir,
      report: (reason) => reports.push(reason),
    }),
    reports,
  };
}

const stored = (dir) => JSON.parse(fs.readFileSync(path.join(dir, 'onboarding.json'), 'utf8'));

function recordedFor(dir, scope) {
  fs.mkdirSync(path.join(dir, 'settings', scope), { recursive: true });
  fs.writeFileSync(
    path.join(dir, 'settings', scope, 'screenshot-settings.json'),
    '{"enabled":true}'
  );
}

test('a new device starts setup at the AI step and keeps that decision', (t) => {
  const dir = tempDir(t);
  assert.deepEqual(createStore(dir).store.get(), { status: 'in_progress', step: 'ai' });
  assert.deepEqual(stored(dir), { status: 'in_progress', step: 'ai' });

  // Recording turned on during setup leaves the preference an existing user has;
  // the next launch must still resume setup instead of skipping it.
  recordedFor(dir, '__logged_out__');
  assert.deepEqual(createStore(dir).store.get(), { status: 'in_progress', step: 'ai' });
});

test('a device where any user already recorded skips setup', (t) => {
  const dir = tempDir(t);
  fs.mkdirSync(path.join(dir, 'settings', '__logged_out__'), { recursive: true });
  recordedFor(dir, 'account-a');
  assert.deepEqual(createStore(dir).store.get(), { status: 'completed' });
  assert.deepEqual(stored(dir), { status: 'completed' });
});

test('setup progress survives a relaunch', (t) => {
  const dir = tempDir(t);
  const { store } = createStore(dir);
  assert.deepEqual(store.set({ status: 'in_progress', step: 'recording' }), {
    status: 'in_progress',
    step: 'recording',
  });
  assert.deepEqual(createStore(dir).store.get(), { status: 'in_progress', step: 'recording' });
  store.set({ status: 'completed' });
  assert.deepEqual(store.get(), { status: 'completed' });
  assert.deepEqual(createStore(dir).store.get(), { status: 'completed' });
});

test('an unreadable state restarts setup, is reported, and is replaced by the next write', (t) => {
  const dir = tempDir(t);
  // An existing user's recording must not turn a corrupt file into "completed".
  recordedFor(dir, 'account-a');
  for (const [contents, reason] of [
    ['{not json', 'malformed_json'],
    ['{"status":"in_progress","step":"welcome"}', 'invalid_state'],
    ['{"status":"completed","step":"done"}', 'invalid_state'],
  ]) {
    fs.writeFileSync(path.join(dir, 'onboarding.json'), contents);
    const { store, reports } = createStore(dir);
    assert.deepEqual(store.get(), { status: 'in_progress', step: 'ai' });
    assert.deepEqual(reports, [reason]);
    assert.equal(fs.readFileSync(path.join(dir, 'onboarding.json'), 'utf8'), contents);
  }
  createStore(dir).store.set({ status: 'in_progress', step: 'apps' });
  assert.deepEqual(stored(dir), { status: 'in_progress', step: 'apps' });
});

/** The desktop features with Electron, the recorder and the welcome request replaced. */
function loadFeatures(t, dir) {
  const calls = [];
  const posted = [];
  const recorder = { on: false, params: null };
  let context;
  const load = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === 'electron') {
      return {
        app: {
          getPath: () => dir,
          getLocale: () => 'en',
          relaunch: () => calls.push('relaunch'),
          quit: () => calls.push('quit'),
        },
        BrowserWindow: { getAllWindows: () => [] },
        systemPreferences: {},
        dialog: {},
        globalShortcut: {},
        ipcMain: {},
        nativeImage: {},
        shell: {},
        clipboard: {},
      };
    }
    if (request === './ipcBootstrap') {
      const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory');
      return {
        startMainIpcRuntime: (options) => ({
          context: (context = buildMainContext(options.context)),
        }),
      };
    }
    if (request === '../screenshot/screenshotSync') {
      return {
        createScreenshotSyncManager: (params) => {
          recorder.params = params;
          return { getStatus: () => recorder.on };
        },
      };
    }
    if (request === './welcomeSuggestion') {
      return { postWelcomeSuggestion: async ({ userId }) => posted.push(userId) };
    }
    return load.call(this, request, parent, isMain);
  };
  const target = require.resolve('../electron/dist/main_runtime/featureRuntime');
  delete require.cache[target];
  let createDesktopFeatureRuntime;
  try {
    ({ createDesktopFeatureRuntime } = require(target));
  } finally {
    Module._load = load;
    delete require.cache[target];
  }
  t.mock.method(globalThis, 'fetch', async () => new Response('{}'));
  const feature = createDesktopFeatureRuntime({
    aiConnection: {},
    frontendDistIndex: '/tmp/index.html',
    localArtifactRoot: dir,
    isDevRuntime: () => false,
    desktopRuntime: { config: null, getBackendUrl: () => 'http://127.0.0.1:61200' },
    notificationWindow: {
      setLocalOwnerIdGetter: () => {},
      setOverlayPlacementGetter: () => {},
      resolveOverlayId: () => null,
      setActionLiveSnapshotGetter: () => {},
      sendResetToAllOverlays: () => {},
      clearActionAssociations: () => {},
    },
    createOrchestrationWS: () => ({ connect: () => {}, disconnect: () => {} }),
    authCoordinator: {},
    localBackendAuthContextController: {},
    supabaseWiring: {
      getLocalOwnerId: () => 'owner-a',
      getRuntimeState: () => ({
        status: 'ready',
        owner: { id: 'owner-a', kind: 'guest' },
        message: null,
      }),
      getSessionManager: () => null,
      getInitializationStatus: () => 'ready',
      signOut: async () => {},
    },
    getLocalApiToken: () => 'token',
    updateUi: {},
    getMainWindow: () => null,
    createMainWindow: () => {},
    resolveUiSettingsPath: () => path.join(dir, 'ui.json'),
    getUiLanguage: () => 'en',
    setUiLanguage: () => {},
    logger: null,
  });
  feature.registerMainIpc();
  return { context, recorder, posted, calls };
}

test('recording started during setup greets only once setup is finished', async (t) => {
  const { context, recorder, posted } = loadFeatures(t, tempDir(t));
  recorder.params.onRecordingStarted('owner-a');
  recorder.on = true;
  context.onboarding.setState({ status: 'in_progress', step: 'apps' });
  await Promise.resolve();
  assert.deepEqual(posted, []);

  context.onboarding.setState({ status: 'completed' });
  await Promise.resolve();
  assert.deepEqual(posted, ['owner-a']);

  // Finished setup greets on the recording start itself, and only a finish greets.
  context.onboarding.setState({ status: 'completed' });
  recorder.params.onRecordingStarted('owner-b');
  await Promise.resolve();
  assert.deepEqual(posted, ['owner-a', 'owner-b']);
});

test('finishing setup with recording off greets nobody', async (t) => {
  const { context, posted } = loadFeatures(t, tempDir(t));
  context.onboarding.setState({ status: 'completed' });
  await Promise.resolve();
  assert.deepEqual(posted, []);
});

test('relaunching schedules the new instance before quitting', (t) => {
  const { context, calls } = loadFeatures(t, tempDir(t));
  context.app.relaunch();
  // Electron starts the scheduled instance only when the quit it follows completes.
  assert.deepEqual(calls, ['relaunch', 'quit']);
});
