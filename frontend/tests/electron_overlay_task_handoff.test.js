const assert = require('assert');
const { test } = require('node:test');

const { registerOverlayHandlers } = require('../electron/dist/ipc/handlers/overlay.js');
const { buildMainContext } = require('../electron/dist/ipc/mainContextFactory.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');
const {
  createIpcSenderSecurity,
  IpcSenderRejectedError,
} = require('../electron/dist/ipc/senderTrust.js');
const { createNotificationIpcHandlerFactory } = require('../electron/notification_window_ipc.js');

// Work a panel started continues in the main window: the main window comes forward on the task,
// and only then does the panel close.

function createMainWindow(steps, { loading = false, loadError = null, loaded = null } = {}) {
  return {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => steps.push('restore'),
    isVisible: () => false,
    show: () => steps.push('show'),
    focus: () => steps.push('focus'),
    loadURL: async (url) => {
      steps.push(['loadURL', url]);
      await loaded;
      if (loadError) throw loadError;
    },
    webContents: {
      isLoading: () => loading,
      send: (channel, payload) => steps.push([channel, payload]),
    },
  };
}

function createPanel(id) {
  let destroyed = false;
  return { id, isDestroyed: () => destroyed, destroy: () => (destroyed = true) };
}

/** The real `showTask` and the real panel registry lookups, over fake windows. */
function createHandoff(steps, { mainWindow = null, createMainWindow: create, panels } = {}) {
  let current = mainWindow;
  const context = buildMainContext({
    getMainWindow: () => current,
    createMainWindow: (hashRoute) => {
      steps.push(['createMainWindow', hashRoute]);
      current = create ? create() : null;
    },
    isDevRuntime: () => false,
    frontendDistIndex: '/tmp/index.html',
    recordSecurityEvent: () => {},
  });
  // Panels by their webContents id, and the registry of open panels by overlay id.
  const open = panels ?? { byWebContents: new Map(), byOverlayId: new Map() };
  if (!panels) {
    const sender = createPanel('sug-1');
    open.byWebContents.set(2, sender);
    open.byOverlayId.set('sug-1', sender);
  }
  const createNotificationIpcHandlers = createNotificationIpcHandlerFactory({
    BrowserWindow: {
      fromWebContents: (webContents) => open.byWebContents.get(webContents.id) ?? null,
    },
    screen: {},
    windows: {
      findOverlayIdByWindow: (win) => win.id,
      getOverlay: (overlayId) => open.byOverlayId.get(overlayId) ?? null,
      hide: (overlayId) => steps.push(['hide', overlayId]),
      getLastOverlayId: () => Array.from(open.byOverlayId.keys()).at(-1) ?? null,
    },
    interactions: {},
  });
  const handlers = new Map();
  registerOverlayHandlers(
    { windows: context.windows, overlay: { createNotificationIpcHandlers }, actions: {} },
    { handle: (channel, handler) => handlers.set(channel, handler), on: () => {} }
  );
  return async (payload) => handlers.get('overlay:openTask')({ sender: { id: 2 } }, payload);
}

test('a loaded main window selects the task in place, then the panel closes', async () => {
  const steps = [];
  const openTask = createHandoff(steps, { mainWindow: createMainWindow(steps) });

  await openTask({ actionId: 'act-1' });

  // A message to the loaded page, never a load that would drop its drafts.
  assert.deepEqual(steps, [
    ['history:showItem', { item: 'action:act-1' }],
    'restore',
    'show',
    'focus',
    ['hide', 'sug-1'],
  ]);
});

test('a main window still loading loads on the task, then the panel closes', async () => {
  const steps = [];
  const openTask = createHandoff(steps, {
    mainWindow: createMainWindow(steps, { loading: true }),
  });

  await openTask({ actionId: 'act-1' });

  assert.deepEqual(steps, [
    ['loadURL', 'file:///tmp/index.html#/history?item=action:act-1'],
    'restore',
    'show',
    'focus',
    ['hide', 'sug-1'],
  ]);
});

test('the panel stays, and the request fails, when the loading main window fails to load', async () => {
  const steps = [];
  const openTask = createHandoff(steps, {
    mainWindow: createMainWindow(steps, {
      loading: true,
      loadError: new Error('ERR_FILE_NOT_FOUND (-6) loading the page'),
    }),
  });

  await assert.rejects(openTask({ actionId: 'act-1' }), /ERR_FILE_NOT_FOUND/);
  assert.equal(
    steps.some((step) => Array.isArray(step) && step[0] === 'hide'),
    false
  );
});

test('a panel closed while the main window loads is not replaced by another in the close', async () => {
  const steps = [];
  let finishLoad;
  const loaded = new Promise((resolve) => (finishLoad = resolve));
  const sender = createPanel('sug-1');
  const panels = {
    byWebContents: new Map([[2, sender]]),
    byOverlayId: new Map([['sug-1', sender]]),
  };
  const openTask = createHandoff(steps, {
    mainWindow: createMainWindow(steps, { loading: true, loaded }),
    panels,
  });

  const request = openTask({ actionId: 'act-1' });
  // Meanwhile the user closes that panel and the same suggestion opens in a new one.
  sender.destroy();
  panels.byWebContents.delete(2);
  const reopened = createPanel('sug-1');
  panels.byWebContents.set(3, reopened);
  panels.byOverlayId.set('sug-1', reopened);
  finishLoad();
  await request;

  assert.equal(
    steps.some((step) => Array.isArray(step) && step[0] === 'hide'),
    false
  );
});

test('without a main window one is created on the task, then the panel closes', async () => {
  const steps = [];
  const openTask = createHandoff(steps, { createMainWindow: () => createMainWindow(steps) });

  await openTask({ actionId: 'act 1' });

  assert.deepEqual(steps, [
    ['createMainWindow', '/history?item=action:act%201'],
    'restore',
    'show',
    'focus',
    ['hide', 'sug-1'],
  ]);
});

test('the panel stays, and the request fails, when no main window can be shown', async () => {
  const steps = [];
  const openTask = createHandoff(steps);
  await assert.rejects(openTask({ actionId: 'act-1' }), /main window could not be opened/);
  assert.deepEqual(steps, [['createMainWindow', '/history?item=action:act-1']]);

  const failing = [];
  const openTaskThrough = createHandoff(failing, {
    createMainWindow: () => {
      throw new Error('window create failed');
    },
  });
  await assert.rejects(openTaskThrough({ actionId: 'act-1' }), /window create failed/);
  assert.deepEqual(failing, [['createMainWindow', '/history?item=action:act-1']]);
});

test('overlay:openTask moves nothing for a malformed request', async () => {
  const steps = [];
  const openTask = createHandoff(steps, { mainWindow: createMainWindow(steps) });

  for (const payload of [
    { actionId: ' act-1' },
    { actionId: '' },
    { actionId: 'act-1', extra: 1 },
    'act-1',
    null,
  ]) {
    await assert.rejects(openTask(payload), IpcValidationError);
  }
  assert.deepEqual(steps, []);
});

test('Only a panel asks main to open its task in the main window', () => {
  const mainFrame = { url: 'http://127.0.0.1:3001/#/history' };
  const mainEvent = { sender: { id: 1, mainFrame }, senderFrame: mainFrame };
  const panelFrame = { url: 'http://127.0.0.1:3001/notification.html' };
  const panelEvent = { sender: { id: 2, mainFrame: panelFrame }, senderFrame: panelFrame };
  const security = createIpcSenderSecurity({
    getMainWindow: () => ({ isDestroyed: () => false, webContents: mainEvent.sender }),
    isDevRuntime: () => true,
    frontendDevOrigin: 'http://127.0.0.1:3001',
    frontendDistIndex: '/app/dist/index.html',
    recordSecurityEvent: () => {},
  });
  security.registerWindow('overlay', panelEvent.sender);

  assert.equal(security.authorize('overlay:openTask', panelEvent), 'overlay');
  assert.throws(
    () => security.authorize('overlay:openTask', mainEvent),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'channel_not_allowed_for_window'
  );
});
