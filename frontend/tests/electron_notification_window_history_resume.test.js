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
    liveResume: {
      kind: 'none',
      processId: null,
      actionId: null,
      commandId: null,
      acceptedAt: null,
    },
    ...overrides,
  };
}

function loadNotificationWindowModule(getOwnerId = () => 'owner-a') {
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
  const display = {
    bounds: { x: 0, y: 0, width: 1440, height: 900 },
    workArea: { x: 0, y: 0, width: 1440, height: 900 },
  };

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
      return appActive ? { id: 'main-window' } : null;
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
      getDisplayMatching: () => display,
    },
  };

  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return fakeElectron;
    return originalLoad.call(this, request, parent, isMain);
  };

  try {
    const notificationWindow = require(targetPath);
    notificationWindow.setUiLanguageGetter(() => 'en');
    notificationWindow.setLocalOwnerIdGetter(getOwnerId);
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
  const main = { isDestroyed: () => false, setFocusable: value => { main.focusable = value; } };
  const handlers = windows.createNotificationIpcHandlers({
    getMainWindow: () => main,
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
  if (process.platform === 'darwin') assert.equal(main.focusable, false);

  owner = null;
  windows.clearForOwnerChange();
  assert.equal(instances.every(win => win.isDestroyed()), true);
  assert.equal(registeredIpcSenders.size, 0);
  assert.equal(windows.resolveOverlayId({ actionId: 'old-action' }), null);
  assert.equal(windows.resolveOverlayId({ processId: 'old-process' }), null);
  if (process.platform === 'darwin') assert.equal(main.focusable, true);
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
  const resumed = [];
  const refreshed = [];
  const handlers = windows.createNotificationIpcHandlers({
    resolveOverlayBootstrap: () => new Promise(resolve => { complete = resolve; }),
    refreshActionConversation: id => refreshed.push(id),
    resumeLiveProcess: request => resumed.push(request),
  });
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  owner = null;
  windows.clearForOwnerChange();
  owner = 'owner-b';
  windows.clearForOwnerChange();
  owner = 'owner-a';
  complete(createBootstrapResponse({
    snapshot: createSnapshot({ actionId: 'old-action' }),
    liveResume: { kind: 'action', processId: 'old-process', actionId: 'old-action', commandId: 'old-command' },
  }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(instances.length, 0);
  assert.deepEqual(refreshed, []);
  assert.deepEqual(resumed, []);
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
    resumeLiveProcess: () => {},
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

test('history overlay restores canonical Action live state after reload and reopen', async () => {
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
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: () => {},
    refreshActionConversation: () => {},
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({ suggestionId, actionId: 'A1', isLive: true }),
      }),
  });

  handlers.onHistoryOpenOverlay(
    {},
    {
      suggestionId: 'S1',
      initialUiState: { expand: true },
    }
  );
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(instances.length, 1);
  assert.equal(instances[0].options.frame, false);
  assert.equal(instances[0].options.transparent, true);
  assert.equal(instances[0].options.resizable, false);
  assert.deepEqual(instances[0].options.webPreferences.additionalArguments, [
    '--pantaray-ui-language=en',
  ]);
  instances[0].webContentsEvents.emit('did-finish-load');
  instances[0].windowEvents.emit('ready-to-show');

  const channels = instances[0].sent.map((entry) => entry.channel);
  assert.deepEqual(channels, ['overlay:snapshot', 'action:conversationUpdated']);
  assert.deepEqual(instances[0].sent[0].payload, {
    snapshot: createSnapshot({ suggestionId: 'S1', actionId: 'A1', isLive: true }),
    initialUiState: { expand: true },
  });
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');
  instances[0].webContentsEvents.emit('did-finish-load');
  assert.equal(instances[0].sent.at(-1).payload.snapshot, actionLiveSnapshot);

  instances[0].destroy();
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));
  instances[1].webContentsEvents.emit('did-finish-load');
  assert.equal(instances[1].sent.at(-1).payload.snapshot, actionLiveSnapshot);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), 'S1');
  assert.equal(instances[0].alwaysOnTop, true);
  assert.equal(instances[0].alwaysOnTopLevel, 'screen-saver');
});

test('terminal history refreshes the exact Action after mapping and queues canonical state until load', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  notificationWindow.setActionLiveSnapshotGetter(() => null);
  const refreshCalls = [];
  const canonicalUpdate = {
    kind: 'action_updated',
    snapshot: {
      actionId: 'A1',
      page: { action: { action_id: 'A1', status: 'success' } },
      transientToolSteps: [],
      approvalBlockers: [],
    },
  };
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: () => {},
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({
          suggestionId,
          actionPhase: 'terminal',
          actionStatus: 'success',
          actionId: 'A1',
          isLive: false,
        }),
      }),
    refreshActionConversation: (actionId) => {
      refreshCalls.push(actionId);
      const overlayId = notificationWindow.resolveOverlayId({ actionId });
      assert.equal(overlayId, 'S1');
      notificationWindow.sendToOverlay(overlayId, 'action:conversationUpdated', canonicalUpdate);
      notificationWindow.cleanupMappingsForAction(actionId);
    },
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(refreshCalls, ['A1']);
  assert.equal(instances.length, 1);
  assert.deepEqual(instances[0].sent, []);
  assert.equal(notificationWindow.resolveOverlayId({ actionId: 'A1' }), null);

  instances[0].webContentsEvents.emit('did-finish-load');

  assert.deepEqual(
    instances[0].sent.map(({ channel }) => channel),
    ['overlay:snapshot', 'action:conversationUpdated']
  );
  assert.equal(instances[0].sent[1].payload, canonicalUpdate);
});

test('history open resumes live action using backend live resume hint', async () => {
  const { notificationWindow } = loadNotificationWindowModule();
  const resumeCalls = [];
  const handlers = notificationWindow.createNotificationIpcHandlers({
    refreshActionConversation: () => {},
    resumeLiveProcess: (payload) => {
      resumeCalls.push(payload);
    },
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({
          suggestionId,
          actionPhase: 'processing',
          actionStatus: 'processing',
          processId: 'P1',
          actionId: 'A1',
          isLive: true,
        }),
        liveResume: {
          kind: 'action',
          processId: 'P1',
          actionId: 'A1',
          commandId: 'CMD1',
          acceptedAt: '2026-03-13T00:00:00Z',
        },
      }),
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1', fromStart: false });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(resumeCalls, [
    {
      kind: 'action',
      suggestionId: 'S1',
      actionId: 'A1',
      commandId: 'CMD1',
      processId: 'P1',
      fromStart: false,
    },
  ]);
});

test('history open resumes live suggestion using backend live resume hint', async () => {
  const { notificationWindow } = loadNotificationWindowModule();
  const resumeCalls = [];
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: (payload) => {
      resumeCalls.push(payload);
    },
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({
        suggestionId,
        snapshot: createSnapshot({
          suggestionId,
          actionPhase: 'idle',
          processId: 'PSUG',
          isLive: true,
        }),
        liveResume: {
          kind: 'suggestion',
          processId: 'PSUG',
          actionId: null,
          commandId: null,
          acceptedAt: null,
        },
      }),
  });

  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(resumeCalls, [
    {
      kind: 'suggestion',
      suggestionId: 'S1',
      actionId: null,
      commandId: null,
      processId: 'PSUG',
      fromStart: true,
    },
  ]);
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
    resumeLiveProcess: () => {},
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
    resumeLiveProcess: () => {},
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
    resumeLiveProcess: () => {},
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
});

test('visible history overlay also suppresses main restore for activate points inside its bounds', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: () => {},
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

  assert.equal(notificationWindow.isVisibleOverlayAtPoint({ x: 720, y: 450 }), true);
});

test('overlays the user opens are centered and grow around that center; suggestions stay top-right', async () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: () => {},
    resolveOverlayBootstrap: async (suggestionId) =>
      createBootstrapResponse({ suggestionId, snapshot: createSnapshot({ suggestionId }) }),
  });
  const resize = (win, height) =>
    handlers.onResizeNotificationWindow({ sender: win.webContents }, { height });

  notificationWindow.showNotification('S1');
  notificationWindow.openStandaloneConversationOverlay('standalone:1');
  notificationWindow.openStandaloneConversationOverlay('conversation:A1', 'A1');
  handlers.onHistoryOpenOverlay({}, { suggestionId: 'S2' });
  await new Promise((resolve) => setImmediate(resolve));
  const [suggestion, ...opened] = instances;

  assert.deepEqual(suggestion.getBounds(), { x: 900, y: 20, width: 520, height: 120 });
  for (const win of opened) {
    assert.deepEqual(win.getBounds(), { x: 460, y: 390, width: 520, height: 120 });
  }

  resize(suggestion, 401);
  assert.equal(suggestion.getBounds().y, 20);

  const [standalone] = opened;
  for (const height of [401, 120, 401, 120]) resize(standalone, height);
  assert.deepEqual(standalone.getBounds(), { x: 460, y: 390, width: 520, height: 120 });
  resize(standalone, 2000);
  assert.deepEqual(standalone.getBounds(), { x: 460, y: 8, width: 520, height: 884 });

  standalone.setPosition(100, 100);
  resize(standalone, 400);
  assert.deepEqual(standalone.getBounds(), { x: 100, y: 342, width: 520, height: 400 });
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
      resumeLiveProcess: () => {},
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

test('history overlay detaches main window from focus candidates while visible', async () => {
  const originalPlatform = process.platform;
  Object.defineProperty(process, 'platform', {
    value: 'darwin',
    configurable: true,
  });
  try {
    const { notificationWindow } = loadNotificationWindowModule();
    const mainWindow = {
      focusable: true,
      setFocusable(value) {
        this.focusable = value;
      },
      isDestroyed() {
        return false;
      },
    };
    const handlers = notificationWindow.createNotificationIpcHandlers({
      resumeLiveProcess: () => {},
      resolveOverlayBootstrap: async (suggestionId) =>
        createBootstrapResponse({
          suggestionId,
          snapshot: createSnapshot({ suggestionId }),
        }),
      getMainWindow: () => mainWindow,
    });

    handlers.onHistoryOpenOverlay({}, { suggestionId: 'S1' });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(mainWindow.focusable, false);

    notificationWindow.hideOverlay('S1');

    assert.equal(mainWindow.focusable, true);
  } finally {
    Object.defineProperty(process, 'platform', {
      value: originalPlatform,
      configurable: true,
    });
  }
});

test('closing a conversation Overlay destroys it and releases its Action association', () => {
  const { notificationWindow, instances } = loadNotificationWindowModule();
  const handlers = notificationWindow.createNotificationIpcHandlers({
    resumeLiveProcess: () => {},
    refreshActionConversation: () => {},
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
