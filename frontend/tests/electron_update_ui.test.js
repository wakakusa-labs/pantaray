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

test('macOS tray icon visual follows recording enabled state, including degraded capture', () => {
  assert.equal(getTrayStatusVisual(null), 'idle');
  assert.equal(getTrayStatusVisual({ kind: 'capturing', screenshotsEnabled: true }), 'capturing');
  assert.equal(getTrayStatusVisual({ kind: 'degraded', screenshotsEnabled: true }), 'capturing');
  assert.equal(
    getTrayStatusVisual({ kind: 'permission_required', screenshotsEnabled: true }),
    'capturing'
  );
  assert.equal(getTrayStatusVisual({ kind: 'unavailable', screenshotsEnabled: true }), 'capturing');
  assert.equal(getTrayStatusVisual({ kind: 'unavailable', screenshotsEnabled: false }), 'idle');
  assert.equal(getTrayStatusVisual({ kind: 'paused', screenshotsEnabled: false }), 'idle');
});

test('tray new conversation action delegates to the overlay owner', () => {
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
    openNewConversationOverlay: () => {
      openCalls += 1;
    },
    setTrayStatusVisual: () => {},
  });

  updateUi.rebuildTrayMenu();
  const newConversationItem = trayMenu.template.find((item) => item.label === 'New task');
  newConversationItem.click();

  assert.equal(openCalls, 1);
  // The tray only displays the shortcut; globalShortcutController stays the sole registrar.
  assert.equal(newConversationItem.accelerator, 'Option+Space');
  assert.equal(newConversationItem.registerAccelerator, false);
});

test('tray new conversation entry omits the shortcut while none is active', () => {
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
    openNewConversationOverlay: () => {},
    setTrayStatusVisual: () => {},
  });

  updateUi.rebuildTrayMenu();
  const newConversationItem = trayMenu.template.find((item) => item.label === 'New task');

  assert.ok(!('accelerator' in newConversationItem));
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
