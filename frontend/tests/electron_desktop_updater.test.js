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
    downloadUpdate: async () => [],
    quitAndInstall: () => {},
  };
}

// Mirrors MacUpdater: every check that finds a version newer than the running app reports it,
// and a download ends with the zip, then Squirrel.Mac's own update-downloaded.
function createFeed({ autoUpdater, squirrelUpdater, calls }) {
  const feed = {
    latest: null,
    stage(version) {
      autoUpdater.emit('update-downloaded', { version });
      squirrelUpdater.emit('update-downloaded');
    },
  };
  autoUpdater.checkForUpdates = async () => {
    calls.push('checkForUpdates');
    autoUpdater.emit('update-available', { version: feed.latest });
  };
  autoUpdater.downloadUpdate = async () => {
    calls.push('downloadUpdate');
    return [];
  };
  autoUpdater.quitAndInstall = () => calls.push('quitAndInstall');
  return feed;
}

function withStagedUpdate({ appVersion, stagedVersion }, fn) {
  const calls = [];
  const app = { isPackaged: true, getVersion: () => appVersion, exit: () => {} };
  const autoUpdater = createAutoUpdater();
  const squirrelUpdater = createEmitter();
  const feed = createFeed({ autoUpdater, squirrelUpdater, calls });
  return withDesktopUpdaterMocks(
    { app, autoUpdater, squirrelUpdater, timers: { setTimeout: () => 1 } },
    async ({ createDesktopUpdater }) => {
      const callbacks = [];
      const hooks = {};
      const updater = createDesktopUpdater({
        logger: null,
        onReadyChanged: () => callbacks.push('readyChanged'),
        onUpdateAvailable: ({ reason, info }) => callbacks.push(['available', reason, info.version]),
        onUpdateNotAvailable: ({ reason, info }) => {
          callbacks.push(['notAvailable', reason, info.version]);
          hooks.onUpdateNotAvailable?.(reason);
        },
      });
      feed.latest = stagedVersion;
      await updater.checkForUpdates('auto');
      feed.stage(stagedVersion);
      calls.length = 0;
      callbacks.length = 0;
      return fn({ updater, feed, calls, callbacks, hooks, autoUpdater, app });
    },
  );
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
      onReadyChanged: () => calls.push(['onReadyChanged']),
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
    ['onReadyChanged'],
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

test('a newer release replaces the staged update, with no restart offered until Squirrel.Mac has it', async () => {
  await withStagedUpdate(
    { appVersion: '0.3.2', stagedVersion: '0.3.3' },
    async ({ updater, feed, calls, callbacks }) => {
      assert.equal(updater.getPendingVersion(), '0.3.3');

      feed.latest = '0.4.0';
      await updater.checkForUpdates('auto');
      assert.deepEqual(calls, ['checkForUpdates', 'downloadUpdate']);

      // Preparing 0.4.0, Squirrel.Mac clears the 0.3.3 it held: a restart would install nothing.
      assert.equal(updater.getUpdateState(), 'downloading');
      assert.equal(updater.isUpdateDownloaded(), false);
      assert.equal(updater.getPendingVersion(), null);
      updater.quitAndInstall();
      assert.deepEqual(calls, ['checkForUpdates', 'downloadUpdate']);

      feed.stage('0.4.0');
      assert.equal(updater.getUpdateState(), 'downloaded');
      assert.equal(updater.getPendingVersion(), '0.4.0');
      assert.deepEqual(callbacks, ['readyChanged', ['available', 'auto', '0.4.0'], 'readyChanged']);
    },
  );
});

test('a check that finds the staged version again downloads nothing', async () => {
  await withStagedUpdate(
    { appVersion: '0.3.2', stagedVersion: '0.3.3' },
    async ({ updater, calls, callbacks }) => {
      await updater.checkForUpdates('auto');
      await updater.checkForUpdates('manual');

      assert.deepEqual(calls, ['checkForUpdates', 'checkForUpdates']);
      assert.deepEqual(callbacks, [
        ['notAvailable', 'auto', '0.3.3'],
        ['notAvailable', 'manual', '0.3.3'],
      ]);
      assert.equal(updater.getUpdateState(), 'downloaded');
      assert.equal(updater.getPendingVersion(), '0.3.3');
    },
  );
});

test('test-channel releases are ordered by their number, and a stable release follows them', async () => {
  await withStagedUpdate(
    { appVersion: '0.4.0-test.1', stagedVersion: '0.4.0-test.9' },
    async ({ updater, feed, calls }) => {
      feed.latest = '0.4.0-test.10';
      await updater.checkForUpdates('auto');
      feed.stage('0.4.0-test.10');
      feed.latest = '0.4.0';
      await updater.checkForUpdates('auto');

      assert.deepEqual(calls, ['checkForUpdates', 'downloadUpdate', 'checkForUpdates', 'downloadUpdate']);
    },
  );
});

test('a failed replacement leaves nothing to install and lets the next check retry', async () => {
  await withStagedUpdate(
    { appVersion: '0.3.2', stagedVersion: '0.3.3' },
    async ({ updater, feed, calls, autoUpdater }) => {
      feed.latest = '0.4.0';
      await updater.checkForUpdates('auto');
      autoUpdater.emit('error', new Error('net::ERR_CONNECTION_RESET'));

      assert.equal(updater.getUpdateState(), 'idle');
      assert.equal(updater.getPendingVersion(), null);
      updater.quitAndInstall();
      await updater.checkForUpdates('auto');

      assert.deepEqual(calls, ['checkForUpdates', 'downloadUpdate', 'checkForUpdates', 'downloadUpdate']);
    },
  );
});

test('checks wait while an update is downloading or being prepared', async () => {
  await withStagedUpdate(
    { appVersion: '0.3.2', stagedVersion: '0.3.3' },
    async ({ updater, feed, calls, autoUpdater }) => {
      feed.latest = '0.4.0';
      await updater.checkForUpdates('auto');
      feed.latest = '0.4.1';
      await updater.checkForUpdates('auto');
      // The zip is in, but Squirrel.Mac is still preparing 0.4.0.
      autoUpdater.emit('update-downloaded', { version: '0.4.0' });
      await updater.checkForUpdates('manual');

      assert.deepEqual(calls, ['checkForUpdates', 'downloadUpdate']);
      assert.equal(updater.getUpdateState(), 'downloading');
    },
  );
});

test('checking from the menu while an update is staged looks for a newer one, then offers the restart', async () => {
  const { createUpdateUiManager } = require('../electron/dist/main_runtime/updateUi.js');
  const { getUpdateMenuCopy } = require('../electron/dist/ui/mainProcessCopy.js');
  const text = getUpdateMenuCopy('en');
  const dialogs = [];
  await withStagedUpdate(
    { appVersion: '0.3.2', stagedVersion: '0.3.3' },
    async ({ updater, calls, hooks, app }) => {
      const updateUi = createUpdateUiManager({
        app,
        dialog: {
          showMessageBox: async (options) => {
            dialogs.push(options);
            return { response: 1 };
          },
        },
        menu: { buildFromTemplate: (template) => ({ template }), setApplicationMenu: () => {} },
        getUiLanguage: () => 'en',
        getDesktopUpdater: () => updater,
        canCheckForUpdatesNow: () => true,
        getTray: () => null,
        getMainWindow: () => null,
        getGlobalShortcutAccelerator: () => null,
        setTrayStatusVisual: () => {},
        onQuitRequested: () => {},
      });
      // Wired the way desktopUpdaterBootstrap does.
      hooks.onUpdateNotAvailable = (reason) => {
        if (reason === 'manual') updateUi.handleNoUpdateAvailable(text);
      };

      updateUi.handleCheckUpdatesClick(text);
      await Promise.resolve();
      assert.deepEqual(calls, ['checkForUpdates']);
    },
  );
  assert.deepEqual(
    dialogs.map(({ title, message }) => [title, message]),
    [[text.updateReadyTitle, `${text.updateReadyBody} (0.3.2 → 0.3.3)`]],
  );
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
      onReadyChanged: () => updateUi.handleUpdateDownloaded(),
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
