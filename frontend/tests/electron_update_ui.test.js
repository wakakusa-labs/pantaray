const assert = require('assert');
const { test } = require('node:test');

const {
  createUpdateUiManager,
  getTrayStatusVisual,
} = require('../electron/dist/main_runtime/updateUi.js');

async function withPlatform(value, fn) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value });
  try {
    await fn();
  } finally {
    Object.defineProperty(process, 'platform', descriptor);
  }
}

const IDLE = { kind: 'idle' };
const ZOOM = { name: 'zoom.us', bundle_id: 'us.zoom.xos' };
const CALL = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y3Z';

test('macOS tray icon visual follows recording enabled state, including degraded capture', () => {
  const visual = (status) => getTrayStatusVisual(status, IDLE);
  assert.equal(visual(null), 'idle');
  assert.equal(visual({ kind: 'capturing', screenshotsEnabled: true }), 'capturing');
  assert.equal(visual({ kind: 'degraded', screenshotsEnabled: true }), 'capturing');
  assert.equal(visual({ kind: 'permission_required', screenshotsEnabled: true }), 'capturing');
  assert.equal(visual({ kind: 'unavailable', screenshotsEnabled: true }), 'capturing');
  assert.equal(visual({ kind: 'unavailable', screenshotsEnabled: false }), 'idle');
  assert.equal(visual({ kind: 'paused', screenshotsEnabled: false }), 'idle');
});

test('the tray shows a transcribed call with Stop, and a passed-up call only while it lasts', () => {
  let trayMenu = null;
  let tip = null;
  const visuals = [];
  const commands = [];
  const updateUi = createUpdateUiManager({
    app: { getVersion: () => '0.0.0' },
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    menu: { buildFromTemplate: (template) => ({ template }), setApplicationMenu: () => {} },
    getUiLanguage: () => 'ja',
    getDesktopUpdater: () => null,
    getTray: () => ({
      setContextMenu: (menu) => {
        trayMenu = menu;
      },
      setToolTip: (text) => {
        tip = text;
      },
      setTitle: () => {},
    }),
    getMainWindow: () => null,
    getGlobalShortcutAccelerator: () => null,
    openAddTodoPanel: () => {},
    setTrayStatusVisual: (visual) => visuals.push(visual),
    recordCall: async (callId) => commands.push(['record', callId]),
    stopCall: async (callId) => commands.push(['stop', callId]),
  });
  const labels = () => trayMenu.template.map((item) => item.label).filter(Boolean);
  const offered = { call_id: CALL, app: ZOOM, site: null };

  updateUi.setMeetingCall({ kind: 'recording', started_at: 'now', ...offered });
  assert.deepEqual(labels().slice(0, 2), ['zoom.us の通話を文字起こし中', '止める']);
  assert.equal(tip, 'zoom.us の通話を文字起こし中');
  assert.equal(visuals.at(-1), 'transcribing');
  trayMenu.template[1].click();

  updateUi.setMeetingCall({ kind: 'not_recording', refused: null, ...offered });
  assert.equal(labels()[0], 'この通話を文字起こしする');
  assert.equal(visuals.at(-1), 'idle');
  trayMenu.template[0].click();
  assert.deepEqual(commands, [
    ['stop', CALL],
    ['record', CALL],
  ]);

  // The chip asks while the consent is open, and a call that ended offers nothing.
  for (const call of [{ kind: 'awaiting_consent', expires_at: 'later', ...offered }, IDLE]) {
    updateUi.setMeetingCall(call);
    assert.ok(!labels().some((label) => label.includes('通話')), call.kind);
  }
});

test('tray add-a-TODO entry opens the add-a-TODO panel', () => {
  let trayMenu = null;
  let openCalls = 0;
  const updateUi = createUpdateUiManager({
    app: { getVersion: () => '0.0.0' },
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    menu: {
      buildFromTemplate: (template) => ({ template }),
      setApplicationMenu: () => {},
    },
    getUiLanguage: () => 'en',
    getDesktopUpdater: () => null,
    getTray: () => ({
      setContextMenu: (menu) => {
        trayMenu = menu;
      },
      setToolTip: () => {},
      setTitle: () => {},
    }),
    getMainWindow: () => null,
    getGlobalShortcutAccelerator: () => 'Option+Space',
    openAddTodoPanel: () => {
      openCalls += 1;
    },
    setTrayStatusVisual: () => {},
  });

  updateUi.rebuildTrayMenu();
  const addTodoItem = trayMenu.template.find((item) => item.label === 'Add a TODO');
  addTodoItem.click();

  assert.equal(openCalls, 1);
  // The tray only displays the shortcut; globalShortcutController stays the sole registrar.
  assert.equal(addTodoItem.accelerator, 'Option+Space');
  assert.equal(addTodoItem.registerAccelerator, false);
});

test('tray add-a-TODO entry omits the shortcut while none is active', () => {
  let trayMenu = null;
  const updateUi = createUpdateUiManager({
    app: { getVersion: () => '0.0.0' },
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    menu: {
      buildFromTemplate: (template) => ({ template }),
      setApplicationMenu: () => {},
    },
    getUiLanguage: () => 'en',
    getDesktopUpdater: () => null,
    getTray: () => ({
      setContextMenu: (menu) => {
        trayMenu = menu;
      },
      setToolTip: () => {},
      setTitle: () => {},
    }),
    getMainWindow: () => null,
    getGlobalShortcutAccelerator: () => null,
    openAddTodoPanel: () => {},
    setTrayStatusVisual: () => {},
  });

  updateUi.rebuildTrayMenu();
  const addTodoItem = trayMenu.template.find((item) => item.label === 'Add a TODO');

  assert.ok(!('accelerator' in addTodoItem));
});

test('macOS tray keeps enabled color and degraded details until recording is paused', async () => {
  await withPlatform('darwin', async () => {
    const trayVisuals = [];
    let nextStatus = null;
    let trayMenu = null;
    let tooltip = null;
    const updateUi = createUpdateUiManager({
      app: {
        name: 'Electron',
        getVersion: () => '0.0.0',
        quit: () => {},
      },
      dialog: {
        showMessageBox: async () => ({ response: 0 }),
      },
      menu: {
        buildFromTemplate: (template) => ({ template }),
        setApplicationMenu: () => {},
      },
      getUiLanguage: () => 'ja',
      getDesktopUpdater: () => null,
      canCheckForUpdatesNow: () => false,
      markManualUpdateCheckPending: () => {},
      consumeManualUpdateCheckPending: () => false,
      getTray: () => ({
        setContextMenu: (menu) => {
          trayMenu = menu;
        },
        setToolTip: (text) => {
          tooltip = text;
        },
        setTitle: () => {},
      }),
      getMainWindow: () => null,
      getGlobalShortcutAccelerator: () => 'Option+Space',
      getCaptureStatusSnapshot: async () => nextStatus,
      startScreenshots: () => true,
      stopScreenshots: () => true,
      setTrayStatusVisual: (visual) => trayVisuals.push(visual),
      onQuitRequested: () => {},
    });

    updateUi.rebuildTrayMenu();
    nextStatus = {
      kind: 'capturing',
      screenshotsEnabled: true,
      activeWindow: { appName: 'Google Chrome', title: 'Docs' },
      browserUrl: null,
      lastCaptureAt: null,
      lastCaptureResult: null,
      reasonLabel: 'capture_allowed',
    };
    await updateUi.refreshCaptureStatus();
    nextStatus = {
      ...nextStatus,
      kind: 'degraded',
      reasonLabel: 'ax',
    };
    await updateUi.refreshCaptureStatus();

    assert.equal(trayMenu.template[0].label, 'Pantaray: 一部の操作を記録できません');
    assert.equal(tooltip, 'Pantaray: 一部の操作を記録できません');
    assert.ok(trayMenu.template.some((item) => item.label === '操作の記録を一時停止'));

    nextStatus = {
      ...nextStatus,
      kind: 'paused',
      screenshotsEnabled: false,
      reasonLabel: 'zanei_paused',
    };
    await updateUi.refreshCaptureStatus();

    assert.deepStrictEqual(trayVisuals, ['idle', 'capturing', 'capturing', 'idle']);
    assert.ok(trayMenu.template.some((item) => item.label === '操作の記録を再開'));
  });
});

test('macOS tray status ignores stale async refresh completions', async () => {
  await withPlatform('darwin', async () => {
    const trayVisuals = [];
    const pendingSnapshots = [];
    const updateUi = createUpdateUiManager({
      app: {
        name: 'Electron',
        getVersion: () => '0.0.0',
        quit: () => {},
      },
      dialog: {
        showMessageBox: async () => ({ response: 0 }),
      },
      menu: {
        buildFromTemplate: (template) => ({ template }),
        setApplicationMenu: () => {},
      },
      getUiLanguage: () => 'ja',
      getDesktopUpdater: () => null,
      canCheckForUpdatesNow: () => false,
      markManualUpdateCheckPending: () => {},
      consumeManualUpdateCheckPending: () => false,
      getTray: () => ({
        setContextMenu: () => {},
        setToolTip: () => {},
        setTitle: () => {},
      }),
      getMainWindow: () => null,
      getGlobalShortcutAccelerator: () => 'Option+Space',
      getCaptureStatusSnapshot: () =>
        new Promise((resolve) => {
          pendingSnapshots.push(resolve);
        }),
      startScreenshots: () => true,
      stopScreenshots: () => true,
      setTrayStatusVisual: (visual) => trayVisuals.push(visual),
      onQuitRequested: () => {},
    });

    const olderRefresh = updateUi.refreshCaptureStatus();
    const newerRefresh = updateUi.refreshCaptureStatus();

    pendingSnapshots[1]({
      kind: 'paused',
      screenshotsEnabled: false,
      activeWindow: { appName: 'Finder', title: 'Desktop' },
      browserUrl: null,
      lastCaptureAt: null,
      lastCaptureResult: null,
      reasonLabel: 'screenshots_paused',
    });
    await newerRefresh;

    pendingSnapshots[0]({
      kind: 'capturing',
      screenshotsEnabled: true,
      activeWindow: { appName: 'Google Chrome', title: 'Docs' },
      browserUrl: null,
      lastCaptureAt: null,
      lastCaptureResult: null,
      reasonLabel: 'capture_allowed',
    });
    await olderRefresh;

    assert.deepStrictEqual(trayVisuals, ['idle']);
  });
});

test('macOS tray starts recording with no filter prerequisite', async () => {
  await withPlatform('darwin', async () => {
    let trayMenu = null;
    let startCalls = 0;
    const updateUi = createUpdateUiManager({
      app: {
        name: 'Electron',
        getVersion: () => '0.0.0',
        quit: () => {},
      },
      dialog: {
        showMessageBox: async () => ({ response: 0 }),
      },
      menu: {
        buildFromTemplate: (template) => ({ template }),
        setApplicationMenu: () => {},
      },
      getUiLanguage: () => 'en',
      getDesktopUpdater: () => null,
      canCheckForUpdatesNow: () => false,
      markManualUpdateCheckPending: () => {},
      consumeManualUpdateCheckPending: () => false,
      getTray: () => ({
        setContextMenu: (menu) => {
          trayMenu = menu;
        },
        setToolTip: () => {},
        setTitle: () => {},
      }),
      getMainWindow: () => null,
      getGlobalShortcutAccelerator: () => 'Option+Space',
      getCaptureStatusSnapshot: async () => ({
        kind: 'paused',
        screenshotsEnabled: false,
        activeWindow: null,
        browserUrl: null,
        lastCaptureAt: null,
        lastCaptureResult: null,
        reasonLabel: 'screenshots_off',
      }),
      startScreenshots: () => {
        startCalls += 1;
        return true;
      },
      stopScreenshots: () => true,
      setTrayStatusVisual: () => {},
      onQuitRequested: () => {},
    });

    await updateUi.refreshCaptureStatus();

    const startItem = trayMenu.template.find((item) => item.label === 'Resume activity recording');
    assert.notEqual(startItem.enabled, false);
    startItem.click();
    assert.equal(startCalls, 1);
  });
});

test('the main window hears whether recording waits on a permission, from the recorder status', async () => {
  const sent = [];
  let nextKind = 'permission_required';
  const updateUi = createUpdateUiManager({
    app: { name: 'Electron', getVersion: () => '0.0.0', quit: () => {} },
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    menu: { buildFromTemplate: (template) => ({ template }), setApplicationMenu: () => {} },
    getUiLanguage: () => 'en',
    getDesktopUpdater: () => null,
    canCheckForUpdatesNow: () => false,
    markManualUpdateCheckPending: () => {},
    consumeManualUpdateCheckPending: () => false,
    getTray: () => null,
    getMainWindow: () => ({
      isDestroyed: () => false,
      webContents: { send: (channel, payload) => sent.push([channel, payload]) },
    }),
    getGlobalShortcutAccelerator: () => null,
    getCaptureStatusSnapshot: async () => ({
      kind: nextKind,
      screenshotsEnabled: true,
      activeWindow: null,
      browserUrl: null,
      lastCaptureAt: null,
      lastCaptureResult: null,
      reasonLabel: `zanei_${nextKind}`,
    }),
    startScreenshots: () => true,
    stopScreenshots: () => true,
    setTrayStatusVisual: () => {},
    onQuitRequested: () => {},
  });

  assert.equal(await updateUi.readPermissionRequired(), true);
  // Any other reason recording is not capturing is not a missing permission.
  nextKind = 'unavailable';
  await updateUi.refreshCaptureStatus();
  assert.equal(await updateUi.readPermissionRequired(), false);
  assert.deepStrictEqual(sent, [
    ['recording:permissionRequiredChanged', true],
    ['recording:permissionRequiredChanged', false],
    ['recording:permissionRequiredChanged', false],
  ]);
});
