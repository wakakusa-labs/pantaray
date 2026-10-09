const assert = require('assert');
const { test } = require('node:test');
const Module = require('module');

process.env.FRONTEND_PORT = '3001';

function createEmitter() {
  const listeners = new Map();
  return {
    on(event, handler) {
      const current = listeners.get(event) || [];
      current.push({ handler, once: false });
      listeners.set(event, current);
    },
    once(event, handler) {
      const current = listeners.get(event) || [];
      current.push({ handler, once: true });
      listeners.set(event, current);
    },
    emit(event, ...args) {
      const current = listeners.get(event) || [];
      if (!current.length) return;
      const keep = [];
      for (const entry of current) {
        entry.handler(...args);
        if (!entry.once) keep.push(entry);
      }
      listeners.set(event, keep);
    },
  };
}

function createSnapshot(overrides = {}) {
  return {
    suggestionId: 'S1',
    commandId: 'CMD1',
    interactionContract: 'action_offer',
    interventionMode: null,
    suggestionText: '提案文',
    reactionState: null,
    reactionTimestamp: null,
    actionPhase: 'idle',
    actionStatus: null,
    processId: null,
    actionId: null,
    updatedAt: '2026-03-13T00:00:00Z',
    lastSequence: 2,
    isLive: false,
    ...overrides,
  };
}

function createBootstrapResponse(overrides = {}) {
  const snapshot =
    overrides && typeof overrides === 'object' && 'snapshot' in overrides
      ? overrides.snapshot
      : createSnapshot();
  return {
    suggestionId: 'S1',
    snapshot,
    lastSequence: snapshot.lastSequence,
    ...overrides,
  };
}

const { DEFAULT_OVERLAY_PLACEMENTS } = require('../electron/dist/settings/overlayPlacement.js');

function loadNotificationWindowModule(
  getOwnerId = () => 'owner-a',
  {
    getPlacements = () => DEFAULT_OVERLAY_PLACEMENTS,
    workArea = { x: 0, y: 0, width: 1440, height: 900 },
    displays = [],
  } = {}
) {
  const originalLoad = Module._load;
  const targetPath = require.resolve('../electron/notification_window.js');
  const factoryPath = require.resolve('../electron/overlay_window_factory.js');
  const ipcPath = require.resolve('../electron/notification_window_ipc.js');
  delete require.cache[targetPath];
  delete require.cache[factoryPath];
  delete require.cache[ipcPath];

  const instances = [];
  const registeredIpcSenders = new Set();
  let appActive = true;
  // The main window, focused while Pantaray is the active app: the History list and its chat.
  const mainWindow = {
    id: 'main-window',
    focusableChanges: [],
    bounds: { x: 100, y: 80, width: 1000, height: 700 },
    normalBounds: { x: 100, y: 80, width: 1000, height: 700 },
    setFocusable(value) {
      this.focusableChanges.push(value);
    },
    getBounds() {
      return { ...this.bounds };
    },
    getNormalBounds() {
      return { ...this.normalBounds };
    },
    isDestroyed: () => false,
  };
  let currentMainWindow = mainWindow;
  const display = {
    bounds: { x: 0, y: 0, width: 1440, height: 900 },
    workArea,
  };
  // A display other than the primary one holds every window whose bounds start inside it.
  const displayMatching = (bounds) =>
    displays.find(
      ({ bounds: area }) =>
        bounds.x >= area.x &&
        bounds.x < area.x + area.width &&
        bounds.y >= area.y &&
        bounds.y < area.y + area.height
    ) ?? display;

  class FakeBrowserWindow {
    constructor(options = {}) {
      this.options = options;
      this.destroyed = false;
      this.visible = options.show !== false;
      this.focusable = options.focusable;
      this.bounds = {
        x: Number(options.x || 0),
        y: Number(options.y || 0),
        width: Number(options.width || 0),
        height: Number(options.height || 0),
      };
      this.windowEvents = createEmitter();
      this.webContentsEvents = createEmitter();
      this.sent = [];
      this.webContents = {
        id: instances.length + 1,
        send: (channel, payload) => {
          this.sent.push({ channel, payload });
        },
        on: (event, handler) => this.webContentsEvents.on(event, handler),
        once: (event, handler) => this.webContentsEvents.once(event, handler),
        closeDevTools: () => {},
      };
      this.webContents.__owner = this;
      instances.push(this);
    }

    // Another Pantaray window (the history list) holds focus while Pantaray is the active app.
    static getFocusedWindow() {
      return appActive ? mainWindow : null;
    }

    static fromWebContents(webContents) {
      return webContents.__owner || null;
    }

    loadURL(url) {
      this.loadedUrl = url;
    }
    show() {
      this.showCalls = (this.showCalls || 0) + 1;
      this.visible = true;
    }
    showInactive() {
      this.showInactiveCalls = (this.showInactiveCalls || 0) + 1;
      this.visible = true;
    }
    focus() {
      this.focusCalls = (this.focusCalls || 0) + 1;
    }
    isMinimized() {
      return Boolean(this.minimized);
    }
    restore() {
      this.restoreCalls = (this.restoreCalls || 0) + 1;
      this.minimized = false;
    }
    hide() {
      this.hideCalls = (this.hideCalls || 0) + 1;
      this.visible = false;
    }
    getBounds() {
      return { ...this.bounds };
    }
    setBounds(bounds) {
      this.bounds = { ...this.bounds, ...bounds };
    }
    setPosition(x, y) {
      this.bounds = { ...this.bounds, x, y };
    }
    moveTop() {}
    setFocusable(value) {
      this.focusable = value;
    }
    setAlwaysOnTop(value, level) {
      this.alwaysOnTop = value;
      this.alwaysOnTopLevel = level;
    }
    setVisibleOnAllWorkspaces(value, options) {
      this.visibleOnAllWorkspaces = value;
      this.visibleOnAllWorkspacesOptions = options;
    }
    isVisible() {
      return this.visible;
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
      this.windowEvents.emit('closed');
    }
    once(event, handler) {
      this.windowEvents.once(event, handler);
    }
    on(event, handler) {
      this.windowEvents.on(event, handler);
    }
  }

  const fakeElectron = {
    BrowserWindow: FakeBrowserWindow,
    app: {
      isPackaged: false,
      focus: () => {},
    },
    screen: {
      getPrimaryDisplay: () => display,
      getDisplayMatching: displayMatching,
    },
  };

  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return fakeElectron;
    return originalLoad.call(this, request, parent, isMain);
  };

  try {
    const notificationWindow = require(targetPath);
    notificationWindow.setUiLanguageGetter(() => 'en');
    notificationWindow.setOverlayPlacementGetter(getPlacements);
    notificationWindow.setLocalOwnerIdGetter(getOwnerId);
    notificationWindow.setMainWindowGetter(() => currentMainWindow);
    notificationWindow.configureIpcWindowSecurity({
      registerWindow: (role, sender) => {
        assert.equal(role, 'overlay');
        registeredIpcSenders.add(sender);
      },
      unregisterWindow: (sender) => registeredIpcSenders.delete(sender),
    });
    return {
      notificationWindow,
      instances,
      registeredIpcSenders,
      mainWindow,
      setMainWindow: (win) => {
        currentMainWindow = win;
      },
      setAppActive: (active) => {
        appActive = active;
      },
    };
  } finally {
    Module._load = originalLoad;
    delete require.cache[targetPath];
    delete require.cache[factoryPath];
    delete require.cache[ipcPath];
  }
}

test('owner cleanup destroys all window kinds and removes snapshots, queues, mappings, and sender access', async () => {
  let owner = 'owner-a';
  const { notificationWindow: windows, instances, registeredIpcSenders } =
    loadNotificationWindowModule(() => owner);
  const handlers = windows.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async () => createBootstrapResponse({ snapshot: createSnapshot({ suggestionId: 'history' }) }),
  });
  windows.showNotification('suggestion');
  windows.openStandaloneConversationOverlay('conversation');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'history' });
  await new Promise(resolve => setImmediate(resolve));
  windows.setOverlaySnapshot('suggestion', { snapshot: createSnapshot({ actionId: 'old-action' }) });
  windows.registerProcessAssociation('old-process', 'suggestion');
  windows.sendToOverlay('suggestion', 'ws:event', { private: 'old event' });
  windows.sendToOverlay('queued-without-window', 'ws:event', { private: 'queued event' });
  assert.equal(instances.length, 3);

  owner = null;
  windows.clearForOwnerChange();
  assert.equal(instances.every(win => win.isDestroyed()), true);
  assert.equal(registeredIpcSenders.size, 0);
  assert.equal(windows.resolveOverlayId({ actionId: 'old-action' }), null);
  assert.equal(windows.resolveOverlayId({ processId: 'old-process' }), null);
  owner = 'owner-b';
  windows.showNotification('suggestion');
  windows.showNotification('queued-without-window');
  for (const win of instances.slice(3)) win.webContentsEvents.emit('did-finish-load');
  assert.deepEqual(instances.slice(3).flatMap(win => win.sent), []);
  windows.sendToAllOverlays('ws:event', { current: true });
  assert.deepEqual(instances.slice(0, 3).flatMap(win => win.sent), []);
});

test('late events from destroyed windows cannot show content or remove a replacement with the same id', () => {
  const { notificationWindow: windows, instances } = loadNotificationWindowModule();
  windows.showNotification('same-id');
  const oldWindow = instances[0];
  const shownBeforeDestruction = oldWindow.showInactiveCalls || 0;
  // Hold closed until after replacement to exercise asynchronous native teardown.
  oldWindow.destroy = () => { oldWindow.destroyed = true; };
  windows.clearForOwnerChange();
  windows.showNotification('same-id');
  windows.setOverlaySnapshot('same-id', { snapshot: createSnapshot({ actionId: 'new-action' }) });
  oldWindow.webContentsEvents.emit('did-finish-load');
  oldWindow.windowEvents.emit('ready-to-show');
  oldWindow.windowEvents.emit('closed');
  assert.deepEqual(oldWindow.sent, []);
  assert.equal(oldWindow.showInactiveCalls || 0, shownBeforeDestruction);
  assert.equal(windows.hasOverlayWindow('same-id'), true);
  assert.equal(windows.resolveOverlayId({ actionId: 'new-action' }), 'same-id');
  instances[1].webContentsEvents.emit('did-finish-load');
  assert.equal(instances[1].sent[0].payload.snapshot.actionId, 'new-action');
});

test('history requests started before an owner switch cannot reopen or resume after returning to the same owner', async () => {
  let owner = 'owner-a';
  const { notificationWindow: windows, instances } = loadNotificationWindowModule(() => owner);
  let complete;
  const opened = [];
  const handlers = windows.createNotificationIpcHandlers({
    resolveOverlayBootstrap: () => new Promise(resolve => { complete = resolve; }),
    openActionConversationOverlay: (id) => opened.push(id),
  });
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  owner = null;
  windows.clearForOwnerChange();
  owner = 'owner-b';
  windows.clearForOwnerChange();
  owner = 'owner-a';
  complete(
    createBootstrapResponse({
      snapshot: createSnapshot({ actionId: 'old-action' }),
    })
  );
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(instances.length, 0);
  assert.deepEqual(opened, []);
  windows.showNotification('S1');
  instances[0].webContentsEvents.emit('did-finish-load');
  assert.deepEqual(instances[0].sent, []);
});

test('unavailable owners cannot open windows or fetch history while same-owner refresh keeps existing windows', async t => {
  let owner = 'owner-a';
  const { notificationWindow: windows, instances } = loadNotificationWindowModule(() => owner);
  windows.openStandaloneConversationOverlay('conversation');
  const existing = instances[0];
  existing.webContentsEvents.emit('did-finish-load');
  existing.windowEvents.emit('ready-to-show');
  owner = null;
  assert.throws(() => windows.openStandaloneConversationOverlay('other'), /Local owner is unavailable/);
  assert.throws(() => windows.showNotification('other'), /Local owner is unavailable/);
  const requests = [];
  const errors = [];
  t.mock.method(console, 'error', (...args) => errors.push(args));
  const handlers = windows.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async id => { requests.push(id); return createBootstrapResponse(); },
  });
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(errors.length, 1);
  assert.match(errors[0][1].message, /Local owner is unavailable/);
  assert.deepEqual(requests, []);
  assert.equal(instances.length, 1);
  assert.equal(existing.isDestroyed(), false);
  owner = 'owner-a';
  assert.equal(windows.openStandaloneConversationOverlay('conversation'), 'focused');
  assert.equal(instances[0], existing);
});

test('standalone conversation reuses the canonical ready Overlay registry and cleans it on destroy', () => {
  const { notificationWindow, instances, registeredIpcSenders } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async () => null,
  });

  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:1'), 'created');
  assert.equal(instances.length, 1);
  assert.equal(new URL(instances[0].loadedUrl).searchParams.get('mode'), 'standalone');
  assert.equal(instances[0].options.resizable, false);
  assert.equal(instances[0].visible, false);
  assert.equal(registeredIpcSenders.has(instances[0].webContents), true);
  assert.equal(
    notificationWindow.resolveOverlayIdForSender(instances[0].webContents),
    'standalone:1'
  );
  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:1'), 'loading');

  instances[0].webContentsEvents.emit('did-finish-load');
  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:1'), 'loading');
  assert.equal(instances[0].showCalls || 0, 0);
  instances[0].windowEvents.emit('ready-to-show');
  assert.equal(instances[0].showCalls, 1);
  assert.equal(instances[0].focusCalls, 1);
  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:1'), 'focused');
  assert.equal(instances.length, 1);
  assert.equal(instances[0].sent.at(-1).channel, 'overlay:focusComposer');

  handlers.onOverlayDragStart({ sender: instances[0].webContents }, { screenX: 1100, screenY: 40 });
  handlers.onOverlayDragMove({ sender: instances[0].webContents }, { screenX: 1110, screenY: 55 });
  assert.equal(notificationWindow.hasRecentOverlayInteraction(), true);
  notificationWindow.registerActionAssociation('A1', 'standalone:1');
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'standalone:1');

  notificationWindow.destroyOverlayWindow('standalone:1');
  assert.equal(registeredIpcSenders.has(instances[0].webContents), false);
  assert.equal(notificationWindow.resolveOverlayIdForSender(instances[0].webContents), null);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), null);
  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 1050, y: 30 }), false);
});

test('a newly loaded panel does not take keyboard focus after another app becomes active', (t) => {
  if (process.platform !== 'darwin') return t.skip('macOS panel behavior');
  const { notificationWindow, instances, setAppActive } = loadNotificationWindowModule();
  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:late'), 'created');
  setAppActive(false);
  instances[0].windowEvents.emit('ready-to-show');
  assert.equal(instances[0].showInactiveCalls, 1);
  assert.equal(instances[0].showCalls || 0, 0);
  assert.equal(instances[0].focusCalls || 0, 0);

  // An explicit reopen of the already loaded panel still focuses it immediately.
  instances[0].webContentsEvents.emit('did-finish-load');
  assert.equal(notificationWindow.openStandaloneConversationOverlay('standalone:late'), 'focused');
  assert.equal(instances[0].focusCalls, 1);
});

test('a delayed history lookup does not focus its existing panel over another app', async (t) => {
  if (process.platform !== 'darwin') return t.skip('macOS panel behavior');
  const { notificationWindow, instances, setAppActive } = loadNotificationWindowModule();
  notificationWindow.showNotification('S1');
  const panel = instances[0];
  panel.windowEvents.emit('ready-to-show');
  const previousInactiveShows = panel.showInactiveCalls;
  let resolveBootstrap;
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: () =>
      new Promise((resolve) => {
        resolveBootstrap = resolve;
      }),
  });
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  setAppActive(false);
  resolveBootstrap(createBootstrapResponse());
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(panel.showInactiveCalls, previousInactiveShows + 1);
  assert.equal(panel.showCalls || 0, 0);
  assert.equal(panel.focusCalls || 0, 0);
});

test('an overlay restores its Action live state each time its page loads', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const actionLiveSnapshot = {
    actionId: 'A1',
    page: null,
    transientToolSteps: [],
    approvalBlockers: [{ processId: 'P1' }],
  };
  notificationWindow.setActionLiveSnapshotGetter((actionId) =>
    actionId === 'A1' ? actionLiveSnapshot : null
  );
  notificationWindow.showNotification('S1');
  const snapshotPayload = {
    snapshot: createSnapshot({ suggestionId: 'S1', actionId: 'A1', isLive: true }),
    initialUiState: { expand: true },
  };
  notificationWindow.setOverlaySnapshot('S1', snapshotPayload);

  instances[0].webContentsEvents.emit('did-finish-load');
  assert.deepEqual(
    instances[0].sent.map((entry) => entry.channel),
    ['overlay:snapshot', 'action:conversationUpdated']
  );
  assert.deepEqual(instances[0].sent[0].payload, snapshotPayload);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');

  // A reload reads it again.
  instances[0].webContentsEvents.emit('did-finish-load');
  assert.equal(instances[0].sent.at(-1).payload.snapshot, actionLiveSnapshot);
});

test('a Suggestion with an Action opened from History or its chat card opens the Action, not a panel', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const opened = [];
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({ suggestionId, actionId: 'A1', reactionState: 'accepted' }),
      }),
    // As conversationOverlay.openActionConversationOverlay does.
    openActionConversationOverlay: (actionId) => {
      opened.push(actionId);
      notificationWindow.registerActionAssociation(actionId, `conversation:${actionId}`);
      notificationWindow.openStandaloneConversationOverlay(`conversation:${actionId}`, actionId);
    },
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1', initialUiState: { expand: true } });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(opened, ['A1']);
  assert.equal(instances.length, 1);
  assert.equal(new URL(instances[0].loadedUrl).searchParams.get('surface'), 'window');
  assert.equal(notificationWindow.hasOverlayWindow('S1'), false);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'conversation:A1');
  // The Suggestion's snapshot is not kept for a window that does not exist.
  notificationWindow.showNotification('S1');
  instances[1].webContentsEvents.emit('did-finish-load');
  assert.deepEqual(instances[1].sent, []);
});

test('a Suggestion that was only shown reopens from History as a panel', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const opened = [];
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
    openActionConversationOverlay: (actionId) => opened.push(actionId),
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(opened, []);
  assert.equal(instances.length, 1);
  assert.equal(instances[0].options.alwaysOnTop, true);
  assert.equal(new URL(instances[0].loadedUrl).searchParams.get('surface'), null);
  instances[0].webContentsEvents.emit('did-finish-load');
  assert.equal(instances[0].sent[0].channel, 'overlay:snapshot');
});

test('live overlay receives overlay:snapshot before ws events', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  notificationWindow.showNotification('S1');
  notificationWindow.setOverlaySnapshot('S1', {
    snapshot: createSnapshot({
      suggestionId: 'S1',
      actionPhase: 'requesting',
      isLive: true,
    }),
  });

  assert.equal(instances.length, 1);
  instances[0].webContentsEvents.emit('did-finish-load');
  instances[0].windowEvents.emit('ready-to-show');

  const channels = instances[0].sent.map((entry) => entry.channel);
  assert.deepEqual(channels, ['overlay:snapshot']);
  assert.equal(instances[0].alwaysOnTop, true);
  assert.equal(instances[0].alwaysOnTopLevel, 'screen-saver');
});

test('overlay broadcast continues after an earlier window send fails', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  notificationWindow.showNotification('S1');
  notificationWindow.showNotification('S2');
  const reset = { kind: 'reset' };
  instances[0].webContents.send = () => {
    throw new Error('renderer closed');
  };

  notificationWindow.sendToAllOverlays('action:conversationUpdated', reset);

  assert.deepEqual(instances[1].sent, [{ channel: 'action:conversationUpdated', payload: reset }]);
});

test('overlay reset replaces queued subject data before renderer load', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const stale = { kind: 'action_updated', snapshot: { actionId: 'old-action' } };
  const reset = { kind: 'reset' };
  notificationWindow.sendToOverlay('S1', 'action:conversationUpdated', stale);
  notificationWindow.sendToOverlay('S1', 'ws:event', { event: 'unrelated' });

  notificationWindow.sendResetToAllOverlays('action:conversationUpdated', reset);
  notificationWindow.showNotification('S1');
  instances[0].webContentsEvents.emit('did-finish-load');

  assert.deepEqual(instances[0].sent, [
    { channel: 'ws:event', payload: { event: 'unrelated' } },
    { channel: 'action:conversationUpdated', payload: reset },
  ]);
});

test('live overlay is selectable without activating on native window creation', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();

  notificationWindow.showNotification('S1');

  assert.equal(instances.length, 1);
  assert.equal(instances[0].options.show, false);
  assert.equal(instances[0].options.focusable, true);
  assert.equal(instances[0].focusable, true);
  assert.equal(instances[0].focusCalls || 0, 0);

  instances[0].windowEvents.emit('ready-to-show');

  assert.equal(instances[0].focusable, true);
  assert.equal(instances[0].focusCalls || 0, 0);
  assert.ok((instances[0].showInactiveCalls || 0) >= 1);
});

test('overlay WebContents registration follows the BrowserWindow lifecycle', () => {
  const { notificationWindow, instances, registeredIpcSenders } = loadNotificationWindowModule();

  notificationWindow.showNotification('S1');

  assert.equal(registeredIpcSenders.has(instances[0].webContents), true);
  assert.equal(notificationWindow.resolveOverlayIdForSender(instances[0].webContents), 'S1');
  notificationWindow.registerActionAssociation('action-1', 'S1');
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'action-1' }), 'S1');
  notificationWindow.clearActionAssociations();
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'action-1' }), null);
  instances[0].destroyed = true;
  assert.equal(notificationWindow.resolveOverlayIdForSender(instances[0].webContents), null);
  instances[0].destroy();
  assert.equal(registeredIpcSenders.has(instances[0].webContents), false);
  assert.equal(notificationWindow.resolveOverlayIdForSender(instances[0].webContents), null);
});

test('visible live overlay suppresses main restore for activate points inside its bounds', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();

  notificationWindow.showNotification('S1');

  assert.equal(instances.length, 1);
  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 1050, y: 30 }), true);
  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 10, y: 10 }), false);

  notificationWindow.hideOverlay('S1');

  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 1050, y: 30 }), false);
});

test('overlay interaction is recorded only for overlay window senders', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async () => null,
  });

  handlers.onOverlayInteraction({ sender: {} });
  assert.equal(notificationWindow.hasRecentOverlayInteraction(), false);

  notificationWindow.showNotification('S1');
  handlers.onOverlayInteraction({ sender: instances[0].webContents });

  assert.equal(notificationWindow.hasRecentOverlayInteraction(), true);
});

test('overlay header drag records interaction and moves only overlay senders', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async () => null,
  });

  handlers.onOverlayDragStart({ sender: {} }, { screenX: 100, screenY: 100 });
  assert.equal(notificationWindow.hasRecentOverlayInteraction(), false);

  notificationWindow.showNotification('S1');
  const startBounds = instances[0].getBounds();
  handlers.onOverlayDragStart({ sender: instances[0].webContents }, { screenX: 1100, screenY: 40 });
  handlers.onOverlayDragMove({ sender: instances[0].webContents }, { screenX: 1115, screenY: 60 });

  assert.equal(notificationWindow.hasRecentOverlayInteraction(), true);
  assert.deepEqual(instances[0].getBounds(), {
    ...startBounds,
    x: startBounds.x + 15,
    y: startBounds.y + 20,
  });

  handlers.onOverlayDragEnd({ sender: instances[0].webContents });
});

test('history open keeps interactive overlay always on top', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({ suggestionId }),
      }),
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(instances.length, 1);
  instances[0].windowEvents.emit('ready-to-show');
  assert.equal(instances[0].alwaysOnTop, true);
  assert.equal(instances[0].alwaysOnTopLevel, 'screen-saver');
  assert.equal(instances[0].focusable, true);
  assert.ok((instances[0].focusCalls || 0) >= 1);
  assert.ok((instances[0].showCalls || 0) >= 1);
  // Joining every Space over full-screen apps turns the whole app into a UI element on macOS
  // (Electron hides the Dock icon to do it): the menu bar then never shows Pantaray, even while
  // its main window is in use.
  assert.equal(instances[0].visibleOnAllWorkspaces, undefined);
});

test('visible history overlay also suppresses main restore for activate points inside its bounds', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({ suggestionId }),
      }),
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(instances.length, 1);
  instances[0].windowEvents.emit('ready-to-show');

  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 1000, y: 60 }), true);
});

test('middle-row overlays are centered and keep that center until the user acts in them; top-row ones grow downward', async () => {
  // History windows default to the top-right now; this covers the middle row a user can choose.
  const { notificationWindow, instances } = loadNotificationWindowModule(undefined, {
    getPlacements: () => ({ ...DEFAULT_OVERLAY_PLACEMENTS, history: { row: 1, column: 2 } }),
  });
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });
  const resize = (win, height) =>
    handlers.onResizeNotificationWindow({ sender: win.webContents }, { height });

  notificationWindow.showNotification('S1');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S2' });
  await new Promise((resolve) => setImmediate(resolve));
  // Tasks the user starts never stack, even over a window already in their cell.
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  notificationWindow.openStandaloneConversationOverlay('standalone:2');
  const [suggestion, history, typed, clicked] = instances;

  assert.deepEqual(suggestion.getBounds(), { x: 900, y: 20, width: 520, height: 120 });
  for (const win of [typed, clicked, history]) {
    assert.deepEqual(win.getBounds(), { x: 460, y: 390, width: 520, height: 120 });
  }

  resize(suggestion, 400);
  assert.equal(suggestion.getBounds().y, 20);

  // Content loading in keeps the window centered, up to the screen edges.
  for (const height of [401, 120, 401]) resize(history, height);
  assert.deepEqual(history.getBounds(), { x: 460, y: 250, width: 520, height: 401 });
  resize(history, 2000);
  assert.deepEqual(history.getBounds(), { x: 460, y: 8, width: 520, height: 884 });

  // After the user's first key or click, the window grows downward from its top.
  typed.webContentsEvents.emit('before-input-event', {}, { key: '@' });
  resize(typed, 320);
  assert.deepEqual(typed.getBounds(), { x: 460, y: 390, width: 520, height: 320 });
  handlers.onOverlayInteraction({ sender: clicked.webContents });
  resize(clicked, 320);
  assert.deepEqual(clicked.getBounds(), { x: 460, y: 390, width: 520, height: 320 });
});

test('macOS overlays have no hidden title bar, whose clicks would activate the app', () => {
  const originalPlatform = process.platform;
  Object.defineProperty(process, 'platform', {
    value: 'darwin',
    configurable: true,
  });
  try {
    const { notificationWindow, instances } = loadNotificationWindowModule();
    notificationWindow.showNotification('S1');

    assert.equal(instances[0].options.type, 'panel');
    assert.equal(instances[0].options.roundedCorners, false);
  } finally {
    Object.defineProperty(process, 'platform', {
      value: originalPlatform,
      configurable: true,
    });
  }
});

test('history overlay is focusable from native window creation on macOS', async () => {
  const originalPlatform = process.platform;
  Object.defineProperty(process, 'platform', {
    value: 'darwin',
    configurable: true,
  });
  try {
    const { notificationWindow, instances } = loadNotificationWindowModule();
    const handlers = notificationWindow.createNotificationIpcHandlers({
      resolveOverlayBootstrap: async (suggestionId) =>
        createBootstrapResponse({
          suggestionId,
          snapshot: createSnapshot({ suggestionId }),
        }),
    });

    handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(instances.length, 1);
    assert.equal(instances[0].options.focusable, true);
    assert.equal(instances[0].focusable, true);
    assert.equal(instances[0].focusCalls || 0, 0);

    instances[0].windowEvents.emit('ready-to-show');

    assert.equal(instances[0].focusable, true);
    assert.ok((instances[0].focusCalls || 0) >= 1);
  } finally {
    Object.defineProperty(process, 'platform', {
      value: originalPlatform,
      configurable: true,
    });
  }
});

test('hide overlay hides window instead of destroying it', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();

  notificationWindow.showNotification('S1');

  assert.equal(instances.length, 1);
  notificationWindow.hideOverlay('S1');

  assert.equal(instances[0].destroyed, false);
  assert.equal(instances[0].visible, false);
  assert.equal(instances[0].hideCalls || 0, 1);
});

test('an open overlay of any kind leaves the main window able to take typing', async () => {
  // The main window holds the chat composer and the History search box. A History overlay used
  // to make it unfocusable on macOS while open, so it took clicks but no keys.
  const originalPlatform = process.platform;
  Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
  try {
    const { notificationWindow, instances, mainWindow } = loadNotificationWindowModule();
    const handlers = notificationWindow.createNotificationIpcHandlers({
      resolveOverlayBootstrap: async (suggestionId) =>
        createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
    });

    handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
    await new Promise((resolve) => setImmediate(resolve));
    notificationWindow.showNotification('S2');
    notificationWindow.openStandaloneConversationOverlay('standalone:1');
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
    for (const win of instances) {
      win.webContentsEvents.emit('did-finish-load');
      win.windowEvents.emit('ready-to-show');
    }
    handlers.onOverlayInteraction({ sender: instances[0].webContents });
    notificationWindow.hideOverlay('S1');
    handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(instances.filter((win) => win.isVisible()).length, 4);
    assert.deepEqual(mainWindow.focusableChanges, []);
  } finally {
    Object.defineProperty(process, 'platform', { value: originalPlatform, configurable: true });
  }
});

test('closing a conversation Overlay destroys it and releases its Action association', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async () => null,
  });

  assert.equal(
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1'),
    'created'
  );
  const conversationWindow = instances[0];
  conversationWindow.webContentsEvents.emit('did-finish-load');
  conversationWindow.windowEvents.emit('ready-to-show');
  notificationWindow.registerActionAssociation('A1', 'conversation:A1');
  assert.equal(notificationWindow.hasOverlayWindow('conversation:A1'), true);

  handlers.onNotificationHide({ sender: conversationWindow.webContents });

  assert.equal(conversationWindow.destroyed, true);
  assert.equal(conversationWindow.hideCalls, undefined);
  assert.equal(notificationWindow.hasOverlayWindow('conversation:A1'), false);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), null);
});

// Every run of a Suggestion's Action carries the Suggestion id, follow-ups and replays included.
const ACTION_EVENT_META = {
  kind: 'action',
  suggestion_id: 'S1',
  process_id: 'P2',
  action_id: 'A1',
  command_id: 'CMD1',
};

function createActionLiveBridge(notificationWindow) {
  const {
    createOrchestrationRendererBridge,
  } = require('../electron/dist/orchestration/orchestrationRendererBridge.js');
  const mainSent = [];
  const bridge = createOrchestrationRendererBridge({
    notificationWindow,
    getMainWindow: () => ({
      isDestroyed: () => false,
      webContents: { send: (channel, payload) => mainSent.push({ channel, payload }) },
    }),
    readLatestActionConversationPage: async (actionId) => ({
      action: {
        action_id: actionId,
        suggestion_id: 'S1',
        status: 'processing',
        latest_run_id: 'P2',
        approved_suggestion: null,
        resumable: false,
      },
      runs: [
        {
          run_id: 'P2',
          status: 'running',
          started_at: '2026-03-08T00:00:01Z',
          completed_at: null,
          completion_event_id: null,
          entries: [],
          final_output: null,
          error: null,
        },
      ],
      unadopted_messages: [],
      next_cursor: null,
    }),
    sendFromRenderer: async () => {},
    requestResume: () => {},
    getUiLanguage: () => 'en',
    normalizeId: (value) => (typeof value === 'string' && value.trim() ? value.trim() : null),
    isConnected: () => true,
    setConnected: () => {},
  });
  return { bridge, mainSent };
}

function actionProcessStarted() {
  return {
    event: 'process_started',
    data: {
      kind: 'action',
      suggestion_id: 'S1',
      process_id: 'P2',
      action_id: 'A1',
      command_id: 'CMD1',
      accepted_at: '2026-03-08T00:00:00Z',
      started_at: '2026-03-08T00:00:01Z',
    },
    meta: ACTION_EVENT_META,
  };
}

function actionToolStep() {
  return {
    event: 'action_step',
    data: {
      action_id: 'A1',
      process_id: 'P2',
      step_kind: 'tool',
      step_id: 'step-1',
      step_number: 1,
      tool_id: 'bash',
      label: 'Run command',
      status: 'processing',
      started_at: '2026-03-08T00:00:02Z',
      completed_at: null,
    },
    meta: { ...ACTION_EVENT_META, logical_run_id: 'P2' },
  };
}

function actionApprovalPaused() {
  return {
    event: 'process_paused',
    data: {
      kind: 'action',
      process_id: 'P2',
      status: 'processing',
      reason: 'approval_pending',
      suggestion_id: 'S1',
      action_id: 'A1',
      command_id: 'CMD1',
      completed_at: '2026-03-08T00:00:03Z',
      approval_blockers: [
        {
          process_id: 'P2',
          action_id: 'A1',
          approval_session_id: 'approval-1',
          tool_request_id: 'tool-request-1',
          tool_id: 'bash',
          intent_class: 'write_outside_workspace',
          command_summary: {},
        },
      ],
    },
    meta: ACTION_EVENT_META,
  };
}

function liveUpdates(sent) {
  return sent
    .filter((entry) => entry.channel === 'action:conversationUpdated')
    .map((entry) => entry.payload.snapshot);
}

function openHistoryConversation(notificationWindow, instances) {
  // conversationOverlay.openActionConversationOverlay binds before it opens.
  notificationWindow.registerActionAssociation('A1', 'conversation:A1');
  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  const conversationWindow = instances.at(-1);
  conversationWindow.webContentsEvents.emit('did-finish-load');
  return conversationWindow;
}

test('a follow-up from a History conversation keeps its approval in that window, not the Suggestion', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const { bridge, mainSent } = createActionLiveBridge(notificationWindow);
  const conversationWindow = openHistoryConversation(notificationWindow, instances);

  // The send binds the sender window, then the relay attached from the start delivers the run.
  notificationWindow.registerActionAssociation('A1', 'conversation:A1');
  bridge.forwardEventToRenderers(actionProcessStarted());
  bridge.forwardEventToRenderers(actionApprovalPaused());
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'conversation:A1');
  assert.equal(liveUpdates(mainSent).at(-1).approvalBlockers.length, 1);
  assert.equal(liveUpdates(conversationWindow.sent).at(-1).approvalBlockers.length, 1);
});

test('a History conversation resumed from the start after a restart keeps its progress and approval', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const { bridge } = createActionLiveBridge(notificationWindow);
  const conversationWindow = openHistoryConversation(notificationWindow, instances);

  // Nothing this session started the run, so the resume replays its process_started.
  bridge.forwardEventToRenderers(actionProcessStarted());
  bridge.forwardEventToRenderers(actionToolStep());
  bridge.forwardEventToRenderers(actionApprovalPaused());
  await new Promise((resolve) => setImmediate(resolve));

  const updates = liveUpdates(conversationWindow.sent);
  assert.equal(
    updates.some((snapshot) => snapshot.transientToolSteps.length === 1),
    true
  );
  assert.equal(updates.at(-1).approvalBlockers.length, 1);
});

test('a server event binds the Action to its Suggestion once the bound window is gone', () => {
  const { notificationWindow } = loadNotificationWindowModule();
  const { bridge } = createActionLiveBridge(notificationWindow);
  notificationWindow.registerActionAssociation('A1', 'conversation:A1');

  bridge.forwardEventToRenderers(actionProcessStarted());

  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');
});

// A macOS work area: below a 33 px menu bar, above a 70 px Dock.
const MAC_WORK_AREA = { x: 0, y: 33, width: 1512, height: 879 };

test('each kind of overlay opens in the cell chosen for it, read when the window opens', async () => {
  let placements = {
    suggestion: { row: 2, column: 0 },
    started: { row: 0, column: 4 },
    history: { row: 2, column: 3 },
  };
  const { notificationWindow, instances } = loadNotificationWindowModule(undefined, {
    getPlacements: () => placements,
    workArea: MAC_WORK_AREA,
  });
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });

  notificationWindow.showNotification('S1');
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S2' });
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S3' });
  await new Promise((resolve) => setImmediate(resolve));
  const position = (win) => ({ x: win.getBounds().x, y: win.getBounds().y });
  assert.deepEqual(instances.map(position), [
    { x: 20, y: 772 },
    { x: 972, y: 53 },
    { x: 798, y: 772 },
    // The second History window stacks upward over the first, which is still loading.
    { x: 798, y: 772 - 132 },
  ]);

  // A Suggestion arriving stacks upward over the one shown in its cell only; a moved setting
  // applies to the next window only.
  for (const win of instances) win.windowEvents.emit('ready-to-show');
  assert.equal(instances.filter((win) => win.isVisible()).length, 4);
  notificationWindow.showNotification('S4');
  assert.deepEqual(position(instances[4]), { x: 20, y: 772 - 132 });
  placements = { ...placements, started: { row: 1, column: 0 } };
  notificationWindow.openStandaloneConversationOverlay('standalone:2');
  assert.deepEqual(position(instances[5]), { x: 20, y: 413 });
  assert.deepEqual(position(instances[1]), { x: 972, y: 53 });
});

test('a bottom-row overlay grows upward from where it is and stays on screen', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule(undefined, {
    getPlacements: () => ({
      suggestion: { row: 2, column: 4 },
      started: { row: 2, column: 2 },
      history: { row: 0, column: 0 },
    }),
    workArea: MAC_WORK_AREA,
  });
  const handlers = notificationWindow.createNotificationIpcHandlers({});
  const resize = (win, height) =>
    handlers.onResizeNotificationWindow({ sender: win.webContents }, { height });
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  notificationWindow.showNotification('S1');
  const [typed, suggestion] = instances;
  const workAreaBottom = 33 + 879;

  resize(typed, 300);
  assert.deepEqual(typed.getBounds(), { x: 496, y: 772 + 120 - 300, width: 520, height: 300 });
  // The user's first key does not turn it downward, off the bottom of the screen.
  typed.webContentsEvents.emit('before-input-event', {}, { key: 'a' });
  handlers.onOverlayInteraction({ sender: typed.webContents });
  resize(typed, 400);
  assert.equal(typed.getBounds().y + typed.getBounds().height, 892);
  resize(typed, 2000);
  assert.deepEqual(typed.getBounds(), { x: 496, y: 33 + 8, width: 520, height: 879 - 16 });

  // Dragged elsewhere, it keeps its new bottom edge.
  typed.setBounds({ y: 200, height: 300 });
  resize(typed, 200);
  assert.deepEqual(typed.getBounds(), { x: 496, y: 300, width: 520, height: 200 });

  resize(suggestion, 500);
  assert.equal(suggestion.getBounds().y + suggestion.getBounds().height, 892);
  assert.ok(suggestion.getBounds().y + 500 <= workAreaBottom);
});

test('a closed Suggestion reopened from History opens in the History cell; a visible one stays put', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule(undefined, {
    getPlacements: () => ({
      suggestion: { row: 0, column: 4 },
      started: { row: 1, column: 2 },
      history: { row: 2, column: 0 },
    }),
    workArea: MAC_WORK_AREA,
  });
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });
  const resize = (win, height) =>
    handlers.onResizeNotificationWindow({ sender: win.webContents }, { height });

  notificationWindow.showNotification('S1');
  const [suggestion] = instances;
  suggestion.windowEvents.emit('ready-to-show');
  resize(suggestion, 300);
  assert.deepEqual(suggestion.getBounds(), { x: 972, y: 53, width: 520, height: 300 });

  // Visible: History brings it forward where the user already sees it.
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(suggestion.getBounds(), { x: 972, y: 53, width: 520, height: 300 });

  // Closed, then reopened from History: bottom-left, keeping its height, growing upward.
  notificationWindow.hideOverlay('S1');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(instances.length, 1);
  assert.deepEqual(suggestion.getBounds(), { x: 20, y: 892 - 300, width: 520, height: 300 });
  resize(suggestion, 400);
  assert.equal(suggestion.getBounds().y + suggestion.getBounds().height, 892);
});

test('by default Suggestions and History windows share the top-right stack without covering each other', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });
  const openFromHistory = async (suggestionId) => {
    handlers.onHistoryOpenOverlay({}, { suggestionId });
    await new Promise((resolve) => setImmediate(resolve));
  };
  const position = (win) => ({ x: win.getBounds().x, y: win.getBounds().y });

  notificationWindow.showNotification('S1');
  // A task the user starts opens centered and is not part of the corner's stack, nor is an
  // ordinary conversation window.
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  await openFromHistory('S2');
  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  const [suggestion, started, history, conversation] = instances;
  assert.deepEqual(position(suggestion), { x: 900, y: 20 });
  assert.deepEqual(position(started), { x: 460, y: 390 });
  assert.deepEqual(position(history), { x: 900, y: 152 });
  assert.deepEqual(position(conversation), { x: 250, y: 130 });

  // A window still loading already holds its slot, so the next one takes the following slot.
  notificationWindow.showNotification('S3');
  assert.deepEqual(position(instances[4]), { x: 900, y: 284 });
  for (const win of instances) win.windowEvents.emit('ready-to-show');

  // A closed Suggestion reopened from History takes the lowest free slot of the corner.
  notificationWindow.hideOverlay('S1');
  notificationWindow.hideOverlay('S3');
  notificationWindow.showNotification('S4');
  assert.deepEqual(position(instances[5]), { x: 900, y: 20 });
  await openFromHistory('S1');
  assert.deepEqual(position(suggestion), { x: 900, y: 284 });
});

test('a window opening in a shared cell takes the slot a closed one freed, not one still in use', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });
  const position = (win) => ({ x: win.getBounds().x, y: win.getBounds().y });

  notificationWindow.showNotification('S1');
  notificationWindow.showNotification('S2');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S3' });
  await new Promise((resolve) => setImmediate(resolve));
  for (const win of instances) win.windowEvents.emit('ready-to-show');
  const [first, middle, last] = instances;
  assert.deepEqual([first, middle, last].map(position), [
    { x: 900, y: 20 },
    { x: 900, y: 152 },
    { x: 900, y: 284 },
  ]);

  // Closing the middle one frees its slot; the closed first one reopened from History takes
  // the lowest free slot, and the next Suggestion the one after, never one in use.
  notificationWindow.hideOverlay('S1');
  notificationWindow.hideOverlay('S2');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(position(first), { x: 900, y: 20 });
  notificationWindow.showNotification('S4');
  assert.deepEqual(position(instances[3]), { x: 900, y: 152 });
  // A window still loading holds its slot too.
  notificationWindow.showNotification('S5');
  assert.deepEqual(position(instances[4]), { x: 900, y: 416 });
});

test('an Action with work opens as an ordinary window, and opening it again focuses that window', () => {
  const { notificationWindow, instances, registeredIpcSenders } = loadNotificationWindowModule();
  notificationWindow.registerActionAssociation('A1', 'conversation:A1');
  assert.equal(
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1'),
    'created'
  );
  const [win] = instances;
  const { options } = win;
  assert.equal(options.frame, true);
  assert.equal(options.transparent, undefined);
  assert.equal(options.alwaysOnTop, undefined);
  assert.equal(options.type, undefined);
  assert.equal(options.skipTaskbar, undefined);
  assert.equal(options.resizable, true);
  assert.equal(options.movable, true);
  assert.equal(options.fullscreenable, true);
  assert.equal(options.acceptFirstMouse, true);
  assert.equal(options.show, false);
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.deepEqual(options.webPreferences.additionalArguments, ['--pantaray-ui-language=en']);
  const url = new URL(win.loadedUrl);
  assert.equal(url.pathname, '/notification.html');
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    mode: 'standalone',
    actionId: 'A1',
    surface: 'window',
  });
  // The main window's size, centered on its display's work area, then 30 px right and down.
  assert.deepEqual(win.getBounds(), { x: 250, y: 130, width: 1000, height: 700 });
  assert.equal(registeredIpcSenders.has(win.webContents), true);
  assert.equal(notificationWindow.resolveOverlayIdForSender(win.webContents), 'conversation:A1');

  assert.equal(
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1'),
    'loading'
  );
  win.webContentsEvents.emit('did-finish-load');
  win.windowEvents.emit('ready-to-show');
  assert.equal(win.showCalls, 1);
  assert.equal(win.isVisible(), true);

  win.minimized = true;
  assert.equal(
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1'),
    'focused'
  );
  assert.equal(instances.length, 1);
  assert.equal(win.restoreCalls, 1);
  assert.equal(win.focusCalls, 1);
  assert.equal(win.alwaysOnTop, undefined);
  assert.equal(win.sent.at(-1).channel, 'overlay:focusComposer');

  // Closed from its title bar or with Command-W.
  win.destroy();
  assert.equal(notificationWindow.hasOverlayWindow('conversation:A1'), false);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), null);
  assert.equal(registeredIpcSenders.has(win.webContents), false);
});

test('an ordinary window on macOS has the main window title bar with its lights on the header', () => {
  const originalPlatform = process.platform;
  Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
  try {
    const { notificationWindow, instances } = loadNotificationWindowModule();
    notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
    assert.equal(instances[0].options.titleBarStyle, 'hiddenInset');
    assert.deepEqual(instances[0].options.trafficLightPosition, { x: 10, y: 14 });
    assert.equal(instances[0].options.type, undefined);
  } finally {
    Object.defineProperty(process, 'platform', { value: originalPlatform, configurable: true });
  }
});

test("an ordinary window takes the main window's size on its display, or the default size without one", () => {
  const secondDisplay = {
    bounds: { x: 1440, y: 0, width: 1920, height: 1080 },
    workArea: { x: 1440, y: 25, width: 1920, height: 1055 },
  };
  const { notificationWindow, instances, mainWindow, setMainWindow } = loadNotificationWindowModule(
    undefined,
    { displays: [secondDisplay] }
  );
  // Zoomed to fill its display: the window keeps the size it returns to.
  mainWindow.bounds = { ...secondDisplay.workArea };
  mainWindow.normalBounds = { x: 1600, y: 100, width: 1200, height: 800 };

  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  assert.deepEqual(instances[0].getBounds(), {
    x: 1440 + 360 + 30,
    y: 25 + 128 + 30,
    width: 1200,
    height: 800,
  });

  setMainWindow(null);
  notificationWindow.openStandaloneConversationOverlay('conversation:A2', 'A2');
  assert.deepEqual(instances[1].getBounds(), { x: 250, y: 130, width: 1000, height: 700 });
});

test('a panel showing an Action is replaced by an ordinary window that keeps its id', () => {
  const { notificationWindow, instances, registeredIpcSenders } = loadNotificationWindowModule();
  notificationWindow.setActionLiveSnapshotGetter(() => null);
  notificationWindow.showNotification('S1');
  const panel = instances[0];
  panel.webContentsEvents.emit('did-finish-load');
  panel.windowEvents.emit('ready-to-show');
  // Accepted in the chat, then closed: the hidden panel still holds the Action.
  notificationWindow.setOverlaySnapshot('S1', {
    snapshot: createSnapshot({ actionId: 'A1', reactionState: 'accepted' }),
  });
  notificationWindow.hideOverlay('S1');
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');

  assert.equal(notificationWindow.openStandaloneConversationOverlay('S1', 'A1'), 'created');
  const win = instances[1];
  assert.equal(panel.isDestroyed(), true);
  assert.equal(registeredIpcSenders.has(panel.webContents), false);
  assert.equal(new URL(win.loadedUrl).searchParams.get('surface'), 'window');
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');
  assert.equal(notificationWindow.resolveOverlayIdForSender(win.webContents), 'S1');

  // What arrives while it loads waits for it instead of going to the destroyed panel.
  const panelMessages = panel.sent.length;
  notificationWindow.dispatchEventToOverlay('ws:event', { meta: { action_id: 'A1' }, data: {} });
  assert.equal(panel.sent.length, panelMessages);
  win.webContentsEvents.emit('did-finish-load');
  assert.deepEqual(
    win.sent.map((entry) => entry.channel),
    ['overlay:snapshot', 'ws:event']
  );
  win.windowEvents.emit('ready-to-show');

  // It stays the Action's one surface: opened again it is focused, and showing the Suggestion
  // puts no panel shell on it.
  assert.equal(notificationWindow.openStandaloneConversationOverlay('S1', 'A1'), 'focused');
  notificationWindow.showNotification('S1');
  assert.equal(instances.length, 2);
  assert.equal(win.alwaysOnTop, undefined);
  assert.equal(win.showInactiveCalls, undefined);
});

test('a visible panel showing an Action is focused as it is, keeping its draft; a hidden one is replaced', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  // A Suggestion accepted in the chat while its panel stays open, and a New task that started
  // work: both may hold an unsent draft in their page.
  notificationWindow.showNotification('S1');
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  const [suggestion, newTask] = instances;
  for (const panel of [suggestion, newTask]) {
    panel.webContentsEvents.emit('did-finish-load');
    panel.windowEvents.emit('ready-to-show');
  }
  notificationWindow.setOverlaySnapshot('S1', {
    snapshot: createSnapshot({ actionId: 'A1', reactionState: 'accepted' }),
  });
  notificationWindow.registerActionAssociation('A2', 'standalone:1');

  for (const [panel, id, actionId] of [
    [suggestion, 'S1', 'A1'],
    [newTask, 'standalone:1', 'A2'],
  ]) {
    const { webContents } = panel;
    const focusCalls = panel.focusCalls || 0;
    assert.equal(notificationWindow.openStandaloneConversationOverlay(id, actionId), 'focused');
    assert.equal(panel.isDestroyed(), false);
    assert.equal(panel.webContents, webContents);
    assert.equal(notificationWindow.resolveOverlayIdForSender(webContents), id);
    assert.equal(panel.focusCalls, focusCalls + 1);
    assert.equal(panel.sent.at(-1).channel, 'overlay:focusComposer');
  }
  assert.equal(instances.length, 2);

  // Once closed, the Suggestion's panel gives way to an ordinary window.
  notificationWindow.hideOverlay('S1');
  assert.equal(notificationWindow.openStandaloneConversationOverlay('S1', 'A1'), 'created');
  assert.equal(suggestion.isDestroyed(), true);
  assert.equal(new URL(instances[2].loadedUrl).searchParams.get('surface'), 'window');
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');
});

test('an ordinary window keeps the size and place the user gives it', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({});
  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  const [win] = instances;
  win.webContentsEvents.emit('did-finish-load');
  win.windowEvents.emit('ready-to-show');
  const bounds = win.getBounds();

  handlers.onResizeNotificationWindow({ sender: win.webContents }, { height: 300 });
  handlers.onResizeNotificationWindow({ sender: {} }, { id: 'conversation:A1', height: 300 });
  handlers.onOverlayDragStart({ sender: win.webContents }, { screenX: 300, screenY: 140 });
  handlers.onOverlayDragMove({ sender: win.webContents }, { screenX: 320, screenY: 160 });
  handlers.onOverlayInteraction({ sender: win.webContents });

  assert.deepEqual(win.getBounds(), bounds);
  assert.equal(notificationWindow.hasRecentOverlayInteraction(), false);
  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 600, y: 400 }), false);
});

test('the overlay IPC channels accept an ordinary window until it closes', () => {
  const { createIpcSenderSecurity } = require('../electron/dist/ipc/senderTrust.js');
  const { buildFrontendDevPageUrl } = require('../electron/dev_frontend_env');
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const security = createIpcSenderSecurity({
    getMainWindow: () => null,
    isDevRuntime: () => true,
    frontendDevOrigin: new URL(buildFrontendDevPageUrl('/notification.html')).origin,
    frontendDistIndex: '/app/dist/index.html',
    recordSecurityEvent: () => {},
  });
  notificationWindow.configureIpcWindowSecurity(security);
  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  const [win] = instances;
  const frame = { url: win.loadedUrl };
  win.webContents.mainFrame = frame;
  const event = { sender: win.webContents, senderFrame: frame };

  for (const channel of [
    'action:submitMessage',
    'action:resume',
    'action:readConversationPage',
    'action:readToolOutputPage',
    'action:attachImage',
    'history:markCompletionViewed',
    'overlay:submitApprovalDecision',
    'overlay:getActionApprovalMode',
    'overlay:setActionApprovalMode',
    'overlay:showChat',
    'notification-hide',
    'clipboard:writeText',
    'ws:getStatus',
    'ui:getLanguage',
  ]) {
    assert.equal(security.authorize(channel, event), 'overlay', channel);
  }
  assert.throws(() => security.authorize('history:openOverlay', event), {
    code: 'channel_not_allowed_for_window',
  });

  win.destroy();
  assert.throws(() => security.authorize('action:submitMessage', event), {
    code: 'window_role_mismatch',
  });
});
