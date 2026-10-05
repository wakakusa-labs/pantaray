const assert = require('node:assert/strict');
const Module = require('node:module');
const { test } = require('node:test');

function withDesktopUpdaterMocks({ app, autoUpdater, squirrelUpdater = createEmitter(), timers }, fn) {
  const sourcePath = require.resolve('../electron/dist/update/desktopUpdater.js');
  delete require.cache[sourcePath];
  const originalLoad = Module._load;
  const originalSetTimeout = global.setTimeout;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') {
      return { app, autoUpdater: squirrelUpdater };
    }
    if (request === 'electron-updater') {
      return { autoUpdater };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  global.setTimeout = timers.setTimeout;
  try {
    return fn(require(sourcePath));
  } finally {
    Module._load = originalLoad;
    global.setTimeout = originalSetTimeout;
    delete require.cache[sourcePath];
  }
}

function createEmitter() {
  const handlers = new Map();
  return {
    on: (event, handler) => handlers.set(event, handler),
    emit: (event, ...args) => handlers.get(event)(...args),
  };
}

function createAutoUpdater() {
  return {
    ...createEmitter(),
    autoDownload: false,
    autoInstallOnAppQuit: false,
    setFeedURL: () => {},
    checkForUpdates: async () => {},
    quitAndInstall: () => {},
  };
}

test('quitAndInstall runs shutdown hook before applying the downloaded update', () => {
  const calls = [];
  const app = {
    isPackaged: true,
    getVersion: () => '0.1.1',
    exit: (code) => calls.push(['exit', code]),
  };
  const autoUpdater = createAutoUpdater();
  const squirrelUpdater = createEmitter();
  autoUpdater.quitAndInstall = (isSilent, isForceRunAfter) => {
    calls.push(['quitAndInstall', isSilent, isForceRunAfter]);
  };
  const timers = {
    setTimeout: (callback, delayMs) => {
      calls.push(['timer', delayMs]);
      callback();
      return 1;
    },
  };

  const logs = [];
  withDesktopUpdaterMocks({ app, autoUpdater, squirrelUpdater, timers }, ({ createDesktopUpdater }) => {
    const updater = createDesktopUpdater({
      logger: {
        info: (name, payload) => logs.push(['info', name, payload]),
        warn: (name, payload) => logs.push(['warn', name, payload]),
        error: (name, payload) => logs.push(['error', name, payload]),
      },
      beforeQuitAndInstall: () => calls.push(['beforeQuitAndInstall']),
    });
    autoUpdater.emit('update-available', { version: '0.1.2' });
    autoUpdater.emit('update-downloaded', { version: '0.1.2' });
    squirrelUpdater.emit('update-downloaded');
    logs.length = 0;

    updater.quitAndInstall();
  });

  assert.deepEqual(calls, [
    ['beforeQuitAndInstall'],
    ['quitAndInstall', false, true],
    ['timer', 6000],
    ['exit', 0],
  ]);
  assert.equal(logs[0][1], 'AUTO_UPDATE_QUIT_INSTALL_REQUESTED');
  assert.equal(logs[1][1], 'AUTO_UPDATE_QUIT_FALLBACK');
});

test('an update is ready only after Squirrel.Mac has prepared it', () => {
  const calls = [];
  const app = { isPackaged: true, getVersion: () => '0.3.0', exit: (code) => calls.push(['exit', code]) };
  const autoUpdater = createAutoUpdater();
  const squirrelUpdater = createEmitter();
  autoUpdater.quitAndInstall = () => calls.push(['quitAndInstall']);
  const timers = {
    setTimeout: (callback, delayMs) => {
      calls.push(['timer', delayMs]);
      return 1;
    },
  };

  withDesktopUpdaterMocks({ app, autoUpdater, squirrelUpdater, timers }, ({ createDesktopUpdater }) => {
    const updater = createDesktopUpdater({
      logger: null,
      onUpdateDownloaded: () => calls.push(['onUpdateDownloaded']),
      beforeQuitAndInstall: () => calls.push(['beforeQuitAndInstall']),
    });
    autoUpdater.emit('update-available', { version: '0.3.1' });
    autoUpdater.emit('update-downloaded', { version: '0.3.1' });

    // electron-updater has the zip, but Squirrel.Mac is still extracting and verifying it.
    assert.equal(updater.getUpdateState(), 'downloading');
    assert.equal(updater.isUpdateDownloaded(), false);
    // Quitting now would kill the app before anything can be installed.
    updater.quitAndInstall();
    assert.deepEqual(calls, []);

    squirrelUpdater.emit('update-downloaded');
    assert.equal(updater.getUpdateState(), 'downloaded');
    assert.equal(updater.isUpdateDownloaded(), true);
    assert.equal(updater.getPendingVersion(), '0.3.1');

    updater.quitAndInstall();
  });

  assert.deepEqual(calls, [
    ['onUpdateDownloaded'],
    ['beforeQuitAndInstall'],
    ['quitAndInstall'],
    ['timer', 6000],
  ]);
});

test('a Squirrel.Mac error before the update is ready is logged and lets the next check retry', async () => {
  const checks = [];
  const logs = [];
  const app = { isPackaged: true, getVersion: () => '0.3.0', exit: () => {} };
  const autoUpdater = createAutoUpdater();
  autoUpdater.checkForUpdates = async () => {
    checks.push('checkForUpdates');
  };

  await withDesktopUpdaterMocks(
    { app, autoUpdater, timers: { setTimeout } },
    async ({ createDesktopUpdater }) => {
      const updater = createDesktopUpdater({
        logger: { info: () => {}, error: (name) => logs.push(name) },
      });
      await updater.checkForUpdates('auto');
      autoUpdater.emit('update-available', { version: '0.3.1' });
      autoUpdater.emit('update-downloaded', { version: '0.3.1' });
      // MacUpdater forwards the native autoUpdater's error as its own.
      autoUpdater.emit('error', new Error('Code signature did not pass validation'));

      assert.equal(updater.getUpdateState(), 'idle');
      assert.equal(updater.isUpdateDownloaded(), false);
      await updater.checkForUpdates('manual');
    },
  );

  assert.deepEqual(logs, ['AUTO_UPDATE_ERR']);
  assert.deepEqual(checks, ['checkForUpdates', 'checkForUpdates']);
});

test('start checks immediately on a packaged build without any auth token and schedules rechecks', () => {
  const calls = [];
  const app = { isPackaged: true, getVersion: () => '0.1.1', exit: () => {} };
  const autoUpdater = createAutoUpdater();
  autoUpdater.checkForUpdates = async () => {
    calls.push('checkForUpdates');
  };
  const intervals = [];
  const originalSetInterval = global.setInterval;
  global.setInterval = (callback, delayMs) => {
    intervals.push(delayMs);
    return 1;
  };
  try {
    withDesktopUpdaterMocks({ app, autoUpdater, timers: { setTimeout } }, ({ createDesktopUpdater }) => {
      const updater = createDesktopUpdater({ logger: null });
      updater.start();
      updater.start();
    });
  } finally {
    global.setInterval = originalSetInterval;
  }
  assert.deepEqual(calls, ['checkForUpdates']);
  assert.deepEqual(intervals, [6 * 60 * 60 * 1000]);
  assert.equal(autoUpdater.allowPrerelease, false);
  assert.equal(autoUpdater.requestHeaders, undefined);
});

test('packaged dev builds have no update feed and never check', () => {
  const calls = [];
  const app = { isPackaged: true, getVersion: () => '0.1.1-dev.3+abc1234', exit: () => {} };
  const autoUpdater = createAutoUpdater();
  autoUpdater.checkForUpdates = async () => {
    calls.push('checkForUpdates');
  };
  const logs = [];
  withDesktopUpdaterMocks(
    { app, autoUpdater, timers: { setTimeout } },
    ({ createDesktopUpdater, hasUpdateFeed, resolveChannelFromVersion }) => {
      assert.equal(hasUpdateFeed(), false);
      assert.deepEqual(
        ['0.1.1', '0.1.1-test.3', '0.1.1-rc.1', '0.1.1-dev.3+abc1234'].map(resolveChannelFromVersion),
        ['stable', 'test', null, null],
      );
      const updater = createDesktopUpdater({ logger: { info: (name) => logs.push(name) } });
      updater.start();
      return updater.checkForUpdates('manual');
    },
  );
  assert.deepEqual(calls, []);
  assert.equal(autoUpdater.allowPrerelease, false);
  assert.ok(logs.includes('AUTO_UPDATE_DISABLED'));
});

test('test-channel versions accept prereleases and unpackaged builds never check', () => {
  const calls = [];
  const app = { isPackaged: false, getVersion: () => '0.1.1-test.3', exit: () => {} };
  const autoUpdater = createAutoUpdater();
  autoUpdater.checkForUpdates = async () => {
    calls.push('checkForUpdates');
  };
  withDesktopUpdaterMocks({ app, autoUpdater, timers: { setTimeout } }, ({ createDesktopUpdater }) => {
    const updater = createDesktopUpdater({ logger: null });
    updater.start();
    return updater.checkForUpdates('manual');
  });
  assert.equal(autoUpdater.allowPrerelease, true);
  assert.deepEqual(calls, []);
});

test('a downloaded update becomes the main window notice and restarts through the menu path', () => {
  const { createUpdateUiManager } = require('../electron/dist/main_runtime/updateUi.js');
  const handlers = new Map();
  const calls = [];
  const sent = [];
  let trayMenu = null;
  const app = { isPackaged: true, getVersion: () => '0.2.1', exit: () => {} };
  const autoUpdater = createAutoUpdater();
  autoUpdater.on = (event, handler) => handlers.set(event, handler);
  autoUpdater.quitAndInstall = () => calls.push('quitAndInstall');
  const squirrelUpdater = createEmitter();

  withDesktopUpdaterMocks({ app, autoUpdater, squirrelUpdater, timers: { setTimeout: () => 1 } }, (module) => {
    let updater = null;
    const updateUi = createUpdateUiManager({
      app,
      menu: { buildFromTemplate: (template) => ({ template }), setApplicationMenu: () => {} },
      getUiLanguage: () => 'en',
      getDesktopUpdater: () => updater,
      getTray: () => ({
        setContextMenu: (menu) => {
          trayMenu = menu;
        },
        setToolTip: () => {},
      }),
      getMainWindow: () => ({
        isDestroyed: () => false,
        webContents: { send: (channel) => sent.push(channel) },
      }),
      getGlobalShortcutAccelerator: () => null,
      setTrayStatusVisual: () => {},
      onQuitRequested: () => calls.push('quitRequested'),
    });
    updater = module.createDesktopUpdater({
      logger: null,
      onUpdateDownloaded: () => updateUi.handleUpdateDownloaded(),
    });

    // Nothing downloaded: no notice, and a restart request does not quit the app.
    handlers.get('update-available')({ version: '0.2.2' });
    assert.equal(updateUi.getReadyNotice(), null);
    updateUi.restartToUpdate();
    assert.deepEqual(calls, []);

    handlers.get('update-downloaded')({ version: '0.2.2' });
    assert.equal(updateUi.getReadyNotice(), null);
    squirrelUpdater.emit('update-downloaded');
    assert.deepEqual(sent, ['update:readyNoticeChanged']);
    assert.deepEqual(updateUi.getReadyNotice(), { version: '0.2.2' });

    trayMenu.template.find((item) => item.label === 'Restart to update').click();
    updateUi.restartToUpdate();
    assert.deepEqual(calls, ['quitRequested', 'quitAndInstall', 'quitRequested', 'quitAndInstall']);
  });
});
