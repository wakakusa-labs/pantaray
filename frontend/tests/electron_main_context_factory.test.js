const assert = require('assert');
const { test } = require('node:test');

const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory.js');

function createMainContextForAuthTests(overrides = {}) {
  const context = buildMainContext({
    ipcMain: {},
    getMainWindow: () => null,
    getAllWindows: () => [],
    isDevRuntime: () => false,
    frontendDistIndex: '/tmp/index.html',
    recordSecurityEvent: () => {},
    saveLoginState: () => {},
    getRuntimeState: () => ({ status: 'ready', message: null, owner: { id: 'user-1', kind: 'account' } }),
    getSupabaseSessionManager: () => ({
      getState: () => ({ isLoggedIn: true, user: { id: 'user-1' } }),
    }),
    signOut: async () => {},
    startBrowserLogin: async () => ({ ok: true }),
    historyFetch: async () => [],
    markCompletionViewed: () => {},
    getUiSettingsPath: () => '/tmp/ui-settings.json',
    getUiLanguage: () => 'ja',
    setUiLanguage: () => {},
    getWorkspaceEditCommandPreference: async () => 'allow',
    setWorkspaceEditCommandPreference: async () => {},
    screenshotSync: {
      getStatus: () => ({ enabled: false }),
      start: () => ({ ok: true }),
      stop: () => ({ ok: true }),
    },
    capturePrivacy: {
      getCaptureSettings: () => ({}),
      updateCaptureSettings: () => ({}),
      getBrowserUrlRules: () => [],
      getIdeFileRules: () => [],
      updateBrowserUrlRules: () => [],
      updateIdeFileRules: () => [],
    },
    openExternalUrl: async () => ({ ok: true }),
    wsSend: async () => {},
    wsAcceptAction: async () => ({ ok: true }),
    wsGetStatus: () => ({ status: 'connected' }),
    enqueueResumeRequest: () => {},
    resolveOverlayBootstrap: async () => null,
    submitApprovalDecision: async () => null,
    ...overrides,
  });
  return { context };
}

test('buildMainContext: signOut waits for the owner transition and maps its failure to IPC', async () => {
  let fail;
  let completed = false;
  const { context } = createMainContextForAuthTests({
    signOut: () => new Promise((resolve, reject) => { fail = reject; }),
  });
  const result = context.auth.signOut().then(value => { completed = true; return value; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(completed, false);
  fail(new Error('clear failed'));
  assert.deepStrictEqual(await result, { ok: false, error: 'clear failed' });
});

test('buildMainContext: auth state is initializing until the Supabase session manager exists', () => {
  const { context } = createMainContextForAuthTests({
    getSupabaseSessionManager: () => null,
    getRuntimeState: () => ({ status: 'unknown', message: null }),
  });

  assert.deepStrictEqual(context.auth.getState(), {
    authStatus: 'initializing',
    isLoggedIn: false,
    user: null,
    runtimeState: { status: 'unknown', message: null },
  });
});

test('buildMainContext: auth state is signed out when session restore failed', () => {
  const { context } = createMainContextForAuthTests({
    getSupabaseSessionManager: () => null,
    getSupabaseSessionInitializationStatus: () => 'failed',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });

  assert.deepStrictEqual(context.auth.getState(), {
    authStatus: 'unauthenticated',
    isLoggedIn: false,
    user: null,
    runtimeState: { status: 'ready', message: null },
  });
});

test('buildMainContext: disabled account login reports a ready guest session', () => {
  const { context } = createMainContextForAuthTests({
    getSupabaseSessionManager: () => null,
    getSupabaseSessionInitializationStatus: () => 'ready',
    getRuntimeState: () => ({
      status: 'ready', message: null, owner: { id: 'guest-owner', kind: 'guest' },
    }),
  });

  assert.deepStrictEqual(context.auth.getState(), {
    authStatus: 'unauthenticated',
    isLoggedIn: false,
    user: null,
    runtimeState: { status: 'ready', message: null, owner: { id: 'guest-owner', kind: 'guest' } },
  });
});

test('buildMainContext: auth state derives authenticated status from the session snapshot', () => {
  const { context } = createMainContextForAuthTests({
    getRuntimeState: () => ({ status: 'syncing', message: null }),
  });

  assert.deepStrictEqual(context.auth.getState(), {
    authStatus: 'authenticated',
    isLoggedIn: true,
    user: { id: 'user-1' },
    runtimeState: { status: 'syncing', message: null },
  });
});

test('buildMainContext: showMainRoute brings a minimized main window forward on the route', () => {
  const calls = [];
  const mainWindow = {
    isDestroyed: () => false,
    isMinimized: () => true,
    isVisible: () => true,
    restore: () => calls.push('restore'),
    show: () => calls.push('show'),
    focus: () => calls.push('focus'),
    loadURL: async (url) => {
      calls.push(url);
    },
  };
  const { context } = createMainContextForAuthTests({ getMainWindow: () => mainWindow });

  context.windows.showMainRoute('/workspace');

  assert.deepEqual(calls, ['file:///tmp/index.html#/workspace', 'restore', 'focus']);
});

test('buildMainContext: ui.setLanguage saves before mutating in-memory language', () => {
  let currentLanguage = 'en';
  const saved = [];
  const windows = [];
  const { context } = createMainContextForAuthTests({
    getUiSettingsPath: () => {
      const dir = require('fs').mkdtempSync(
        require('path').join(require('os').tmpdir(), 'pantaray-ui-main-')
      );
      return require('path').join(dir, 'ui-settings.json');
    },
    getUiLanguage: () => currentLanguage,
    setUiLanguage: (lang) => {
      saved.push(lang);
      currentLanguage = lang;
    },
    getAllWindows: () => windows,
  });

  assert.equal(context.ui.setLanguage('ja'), 'ja');
  assert.deepEqual(saved, ['ja']);
  assert.equal(currentLanguage, 'ja');
});

test('buildMainContext: ui.setLanguage does not mutate language when persistence fails', () => {
  let currentLanguage = 'en';
  let setCalls = 0;
  const fs = require('fs');
  const os = require('os');
  const path = require('path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-ui-main-fail-'));
  const parentFile = path.join(dir, 'not-a-directory');
  fs.writeFileSync(parentFile, 'x', 'utf8');
  const { context } = createMainContextForAuthTests({
    getUiSettingsPath: () => path.join(parentFile, 'ui-settings.json'),
    getUiLanguage: () => currentLanguage,
    setUiLanguage: (lang) => {
      setCalls += 1;
      currentLanguage = lang;
    },
  });

  assert.throws(() => context.ui.setLanguage('ja'));
  assert.equal(setCalls, 0);
  assert.equal(currentLanguage, 'en');
});

test('owner-scoped settings and recording IPC reject while no confirmed owner is published', async () => {
  for (const status of ['unknown', 'syncing', 'degraded', 'ready']) {
    const { context } = createMainContextForAuthTests({
      getRuntimeState: () => ({ status, message: null, owner: null }),
      getUiSettingsPath: () => { throw new Error('old settings path must not be accessed'); },
    });
    assert.throws(() => context.ui.setLanguage('en'), /Local owner is unavailable/);
    assert.throws(() => context.privacy.getCaptureSettings(), /Local owner is unavailable/);
    assert.throws(() => context.privacy.getIdeFileRules(), /Local owner is unavailable/);
    assert.throws(() => context.screenshot.getStatus(), /Local owner is unavailable/);
    assert.throws(() => context.screenshot.dismissIntro('user-1'), /Local owner is unavailable/);
    for (const operation of [
      () => context.privacy.updateCaptureSettings({}),
      () => context.privacy.updateIdeFileRules([]),
      () => context.privacy.setCaptureEditing({ kind: 'begin', ownerId: 'user-1', sessionId: 'editor' }),
      () => context.screenshot.start(),
      () => context.screenshot.stop(),
    ]) await assert.rejects(operation, /Local owner is unavailable/);
  }
});

test('only the owner the recording screen was shown to can answer it with "later"', () => {
  // The screen waits on a macOS permission granted in System Settings, so its start can
  // outlive the owner it was shown to. Main holds one gate for the whole app run, so an
  // answer carried by that late reply must not drop what the current owner is waiting on.
  let dismissed = 0;
  const gate = { osPermissionsGranted: false, introRequested: true, introDismissed: false };
  const { context } = createMainContextForAuthTests({
    getRuntimeState: () => ({ status: 'ready', message: null, owner: { id: 'account-b', kind: 'account' } }),
    readCaptureGateState: () => ({ ...gate }),
    dismissRecordingIntro: () => {
      dismissed += 1;
      Object.assign(gate, { introRequested: false, introDismissed: true });
      return { ...gate };
    },
  });

  assert.deepEqual(context.screenshot.dismissIntro('account-a'), {
    osPermissionsGranted: false, introRequested: true, introDismissed: false,
  });
  assert.equal(dismissed, 0);

  assert.deepEqual(context.screenshot.dismissIntro('account-b'), {
    osPermissionsGranted: false, introRequested: false, introDismissed: true,
  });
  assert.equal(dismissed, 1);
});

for (const boundary of ['before_write', 'after_write']) {
  test(`privacy mutation rechecks its owner ${boundary} and never publishes stale settings`, async t => {
    const fs = require('node:fs');
    const dir = fs.mkdtempSync(require('node:path').join(require('node:os').tmpdir(), 'owner-privacy-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const { createCapturePrivacyManager } = require('../electron/dist/privacy/capturePrivacy');
    const privacy = createCapturePrivacyManager({ userDataDir: dir, initialUserId: 'alice', resolveAppBundleId: () => null });
    const original = privacy.updateCaptureSettings({ apps: { mode: 'include_only', entries: [] } });
    let owner = { id: 'alice', kind: 'account' };
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const broadcasts = [];
    const { context } = createMainContextForAuthTests({
      getRuntimeState: () => ({ status: 'ready', message: null, owner }),
      capturePrivacy: privacy,
      screenshotSync: { updatePrivacy: async update => {
        if (boundary === 'before_write') await held;
        const result = update();
        if (boundary === 'after_write') await held;
        return result;
      } },
      getMainWindow: () => ({ isDestroyed: () => false, webContents: { send: (...args) => broadcasts.push(args) } }),
    });
    const update = context.privacy.updateCaptureSettings({ apps: { mode: 'exclude', entries: [] } });
    owner = { id: 'bob', kind: 'account' };
    const bobOriginal = privacy.setSettingsScope('bob');
    const rejected = assert.rejects(update, /Local owner (is unavailable|changed)/);
    release();
    await rejected;
    assert.deepEqual(privacy.getCaptureSettings(), bobOriginal);
    assert.deepEqual(broadcasts, []);
    const alice = privacy.setSettingsScope('alice');
    assert.equal(alice.apps.mode, boundary === 'before_write' ? original.apps.mode : 'exclude');
  });
}

for (const operation of ['updateCaptureSettings', 'updateIdeFileRules']) {
  test(`a destroyed renderer cannot turn a saved ${operation} into a failed mutation`, async () => {
    const saved = [];
    let changed = 0;
    const { context } = createMainContextForAuthTests({
      screenshotSync: { updatePrivacy: async update => update() },
      capturePrivacy: {
        [operation]: value => { saved.push(value); return value; },
        getCaptureSettings: () => ({ apps: { mode: 'exclude', entries: [] } }),
      },
      getMainWindow: () => ({ isDestroyed: () => false, webContents: {
        send: () => { throw new Error('Object has been destroyed'); },
      } }),
      onCaptureSettingsChanged: () => { changed++; },
    });
    const value = operation === 'updateIdeFileRules' ? [] : { apps: { mode: 'exclude', entries: [] } };
    assert.deepEqual(await context.privacy[operation](value), value);
    assert.deepEqual(saved, [value]);
    assert.equal(changed, 1);
  });
}

test('editing cleanup reaches the recorder after the local owner becomes unavailable', async () => {
  let owner = { id: 'alice', kind: 'account' };
  const requests = [];
  const { context } = createMainContextForAuthTests({
    getRuntimeState: () => ({ status: 'ready', message: null, owner }),
    screenshotSync: { setCaptureEditing: async request => { requests.push(request); return true; } },
  });
  assert.equal(await context.privacy.setCaptureEditing({ kind: 'begin', ownerId: 'alice', sessionId: 'editor' }), true);
  owner = null;
  await context.privacy.setCaptureEditing({ kind: 'end', sessionId: 'editor' });
  assert.deepEqual(requests.map(request => request.kind), ['begin', 'end']);
  assert.equal(requests[0].ownerId, 'alice');
  assert.equal(requests[1].sessionId, requests[0].sessionId);
});


test('editing cannot be started for a different owner than the current IPC owner', async () => {
  const { context } = createMainContextForAuthTests();
  await assert.rejects(async () => context.privacy.setCaptureEditing({ kind: 'begin', ownerId: 'old-owner', sessionId: 'editor' }), /Local owner changed/);
});
