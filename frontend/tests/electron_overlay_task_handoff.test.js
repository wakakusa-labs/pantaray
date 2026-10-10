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

// Work a panel started continues in the main window: the panel closes, the main window comes
// forward, and its renderer is told which task to select.

function registerWithPanels(steps) {
  const panels = new Map([
    [2, { id: 'sug-1', isDestroyed: () => false }],
    [3, { id: 'standalone:new', isDestroyed: () => false }],
  ]);
  // The real hide path the panel's close button takes, over fake windows.
  const createNotificationIpcHandlers = createNotificationIpcHandlerFactory({
    BrowserWindow: { fromWebContents: (webContents) => panels.get(webContents.id) ?? null },
    screen: {},
    windows: {
      findOverlayIdByWindow: (win) => win.id,
      hide: (overlayId) => steps.push(['hide', overlayId]),
      getLastOverlayId: () => null,
    },
    interactions: {},
  });
  const handlers = new Map();
  registerOverlayHandlers(
    {
      windows: { showTask: (actionId) => steps.push(['showTask', actionId]) },
      overlay: { createNotificationIpcHandlers },
      actions: {},
    },
    { handle: (channel, handler) => handlers.set(channel, handler), on: () => {} }
  );
  return (senderId, payload) =>
    handlers.get('overlay:openTask')({ sender: { id: senderId } }, payload);
}

test('overlay:openTask hides the sending panel, then brings the main window to the task', async () => {
  const steps = [];
  const openTask = registerWithPanels(steps);

  await openTask(2, { actionId: 'act-1' });
  await openTask(3, { actionId: 'act-2' });

  assert.deepEqual(steps, [
    ['hide', 'sug-1'],
    ['showTask', 'act-1'],
    ['hide', 'standalone:new'],
    ['showTask', 'act-2'],
  ]);
});

test('overlay:openTask moves nothing for a malformed request', async () => {
  const steps = [];
  const openTask = registerWithPanels(steps);

  for (const payload of [
    { actionId: ' act-1' },
    { actionId: '' },
    { actionId: 'act-1', extra: 1 },
    'act-1',
    null,
  ]) {
    await assert.rejects(async () => openTask(2, payload), IpcValidationError);
  }
  assert.deepEqual(steps, []);
});

function createHiddenMinimizedMainWindow(steps) {
  return {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => steps.push('restore'),
    isVisible: () => false,
    show: () => steps.push('show'),
    focus: () => steps.push('focus'),
    loadURL: async () => steps.push('loadURL'),
    webContents: { send: (channel, payload) => steps.push([channel, payload]) },
  };
}

function buildContext(getMainWindow) {
  return buildMainContext({
    getMainWindow,
    isDevRuntime: () => false,
    frontendDistIndex: '/tmp/index.html',
    recordSecurityEvent: () => {},
  });
}

test('showTask shows a hidden or minimized main window, focuses it, and selects the task in place', () => {
  const steps = [];
  const context = buildContext(() => createHiddenMinimizedMainWindow(steps));

  context.windows.showTask('act-1');

  // Selected by a message to the loaded page, never a load that would drop its drafts.
  assert.deepEqual(steps, [
    'restore',
    'show',
    'focus',
    ['history:showItem', { item: 'action:act-1' }],
  ]);
});

test('showTask does nothing without a main window', () => {
  const context = buildContext(() => null);
  assert.doesNotThrow(() => context.windows.showTask('act-1'));
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
