const { BrowserWindow, screen } = require('electron');
const { createOverlayActivationTracker } = require('./overlay_activation_tracker');
const { createOverlayDragController } = require('./overlay_drag_controller');
const { createAuxiliaryWindowIpcSecurity } = require('./auxiliary_window_ipc_security');
const {
  createNotificationIpcHandlerFactory,
  resizeOverlayWindow,
} = require('./notification_window_ipc');
const {
  createOverlayWindowFactory,
  applyOverlayShellMode,
  moveOverlayWindowToCell,
  showInteractiveOverlayWindow,
} = require('./overlay_window_factory');

const overlayWindows = new Map(); // key: suggestionId, value: BrowserWindow
let lastOverlayId = null;
const overlayState = new Map(); // id -> { ready, queue, shellMode }
const overlaySnapshotPayloads = new Map(); // id -> latest overlay:snapshot payload
const processToSuggestion = new Map(); // processId -> suggestionId
const actionToOverlayId = new Map(); // actionId -> Overlay id
const historyOverlayIds = new Set();
// Overlay ids whose window this app opened for one conversation. Their close
// control destroys the window instead of hiding it, so a session cannot
// accumulate invisible conversation renderers.
const conversationOverlayIds = new Set();
let getMainWindowForOverlayIsolation = null;
let getUiLanguage = null;
let getOverlayPlacements = null;
let getActionLiveSnapshot = null;
let getLocalOwnerId = null;
let ownerGeneration = 0;

const PASSIVE_SHELL_MODE = 'passive';
const INTERACTIVE_SHELL_MODE = 'interactive';
const overlayActivationTracker = createOverlayActivationTracker();
const overlayDragController = createOverlayDragController({
  BrowserWindow,
  screen,
  activationTracker: overlayActivationTracker,
});
const auxiliaryWindowIpcSecurity = createAuxiliaryWindowIpcSecurity();
const overlayWindowFactory = createOverlayWindowFactory({
  getUiLanguage: currentUiLanguage,
  registerWindow: (win) => auxiliaryWindowIpcSecurity.registerWindow('overlay', win),
});

function setUiLanguageGetter(getter) {
  if (typeof getter !== 'function') {
    throw new Error('setUiLanguageGetter requires a function.');
  }
  getUiLanguage = getter;
}

function setOverlayPlacementGetter(getter) {
  if (typeof getter !== 'function') {
    throw new Error('setOverlayPlacementGetter requires a function.');
  }
  getOverlayPlacements = getter;
}

function overlayCellFor(placementKind) {
  if (!getOverlayPlacements) {
    throw new Error('Overlay placement getter is required before creating overlay windows.');
  }
  return getOverlayPlacements()[placementKind];
}

// A conversation with an Action is one reopened from History (its list or its chat); a new
// one is started by the user.
function standalonePlacementKind(actionId) {
  return actionId ? 'history' : 'started';
}

// The cell and stack slot each window was last placed in.
const overlayWindowSlots = new WeakMap();

// Suggestions and History windows take the lowest free slot of their cell's stack, so the two
// never open exactly on top of each other when they share a cell (by default, the top-right).
// A slot is held by a window in that cell that is shown or still loading to be shown; a closed
// (hidden) one frees it. A task the user starts always opens in its cell itself.
function stackPlacement(placementKind) {
  const cell = overlayCellFor(placementKind);
  if (placementKind === 'started') return { cell, stackIndex: 0 };
  const heldSlots = new Set();
  for (const [id, win] of overlayWindows) {
    const placed = overlayWindowSlots.get(win);
    if (
      placed &&
      placed.cell.row === cell.row &&
      placed.cell.column === cell.column &&
      !win.isDestroyed() &&
      (win.isVisible() || overlayState.get(id)?.readyToShow === false)
    ) {
      heldSlots.add(placed.stackIndex);
    }
  }
  let stackIndex = 0;
  while (heldSlots.has(stackIndex)) stackIndex += 1;
  return { cell, stackIndex };
}

// A closed Suggestion is hidden, not destroyed, so reopening it from History reuses its window;
// a hidden window opens where its new kind opens, and a visible one stays where the user sees it.
function placeHiddenOverlayWindow(win, placementKind) {
  if (win.isVisible()) return;
  const placement = stackPlacement(placementKind);
  moveOverlayWindowToCell(win, placement.cell, placement.stackIndex);
  overlayWindowSlots.set(win, placement);
  resizeOverlayWindow(screen, win, win.getBounds().height);
}

function setActionLiveSnapshotGetter(getter) {
  if (typeof getter !== 'function') {
    throw new Error('setActionLiveSnapshotGetter requires a function.');
  }
  getActionLiveSnapshot = getter;
}

function currentUiLanguage() {
  if (!getUiLanguage) {
    throw new Error('UI language getter is required before creating overlay windows.');
  }
  const language = getUiLanguage();
  if (language !== 'en' && language !== 'ja') {
    throw new Error('UI language getter returned an invalid value.');
  }
  return language;
}

function normalizeId(value) {
  if (!value && value !== 0) return null;
  const str = String(value).trim();
  return str.length ? str : null;
}

function getOverlayRuntimeState(id) {
  const normalizedId = normalizeId(id);
  if (!normalizedId) return null;
  const existing = overlayState.get(normalizedId);
  if (existing) {
    return existing;
  }
  const created = {
    ready: false,
    readyToShow: false,
    queue: [],
    shellMode: PASSIVE_SHELL_MODE,
  };
  overlayState.set(normalizedId, created);
  return created;
}

function setLocalOwnerIdGetter(getter) {
  if (typeof getter !== 'function') throw new TypeError('Local owner getter is required.');
  getLocalOwnerId = getter;
}

function readOwnerScope() {
  const ownerId = getLocalOwnerId?.();
  if (!ownerId) throw new Error('Local owner is unavailable.');
  return { ownerId, generation: ownerGeneration };
}

function isOwnerScopeCurrent(scope) {
  return scope.generation === ownerGeneration && scope.ownerId === getLocalOwnerId();
}

function clearForOwnerChange() {
  ownerGeneration += 1;
  for (const win of overlayWindows.values()) {
    if (!win.isDestroyed()) win.destroy();
  }
  overlayWindows.clear();
  overlayState.clear();
  overlaySnapshotPayloads.clear();
  processToSuggestion.clear();
  actionToOverlayId.clear();
  historyOverlayIds.clear();
  conversationOverlayIds.clear();
  lastOverlayId = null;
  syncMainWindowFocusableState();
}

function findOverlayIdByWindow(targetWin) {
  if (!targetWin) return null;
  for (const [id, win] of overlayWindows.entries()) {
    if (win === targetWin) {
      return id;
    }
  }
  return null;
}

function resolveOverlayIdForSender(sender) {
  if (!sender) return null;
  for (const [id, win] of overlayWindows.entries()) {
    if (!win.isDestroyed() && win.webContents === sender) return id;
  }
  return null;
}

function syncMainWindowFocusableState() {
  const getMainWindow = getMainWindowForOverlayIsolation;
  if (typeof getMainWindow !== 'function') return;
  if (process.platform !== 'darwin') return;
  const shouldAllowFocus = historyOverlayIds.size === 0;
  try {
    const mainWindow = getMainWindow();
    if (!mainWindow || (typeof mainWindow.isDestroyed === 'function' && mainWindow.isDestroyed()))
      return;
    if (typeof mainWindow.setFocusable === 'function') {
      mainWindow.setFocusable(shouldAllowFocus);
    }
  } catch {}
}

function flushOverlayQueue(id) {
  const normalizedId = normalizeId(id);
  if (!normalizedId) return;
  const win = overlayWindows.get(normalizedId);
  const runtime = overlayState.get(normalizedId);
  if (!win || !runtime || !runtime.ready || win.isDestroyed()) return;
  const pending = runtime.queue.slice();
  runtime.queue = [];
  for (const message of pending) {
    try {
      win.webContents.send(message.channel, message.payload);
    } catch {}
  }
}

function deliverOverlayState(id, win, runtime) {
  runtime.ready = true;
  const payload = overlaySnapshotPayloads.get(id);
  if (payload) {
    try {
      win.webContents.send('overlay:snapshot', payload);
    } catch {}
  }
  flushOverlayQueue(id);
  const actionId = normalizeId(payload?.snapshot?.actionId);
  const snapshot = actionId && getActionLiveSnapshot?.(actionId);
  if (snapshot)
    enqueueOverlayMessage(id, 'action:conversationUpdated', { kind: 'action_updated', snapshot });
}

function enqueueOverlayMessage(id, channel, payload) {
  const normalizedId = normalizeId(id);
  if (!normalizedId) return;
  const runtime = getOverlayRuntimeState(normalizedId);
  if (!runtime) return;
  if (runtime.ready) {
    const win = overlayWindows.get(normalizedId);
    if (win && !win.isDestroyed()) {
      try {
        win.webContents.send(channel, payload);
        return;
      } catch {}
    }
  }
  runtime.queue.push({ channel, payload });
}

function registerProcessAssociation(processId, suggestionId) {
  const pid = normalizeId(processId);
  const sid = normalizeId(suggestionId);
  if (!pid || !sid) return;
  processToSuggestion.set(pid, sid);
}

// An explicit open or send binds the Action to the window the user is looking at.
function registerActionAssociation(actionId, overlayId) {
  const aid = normalizeId(actionId);
  const oid = normalizeId(overlayId);
  if (!aid || !oid) return;
  actionToOverlayId.set(aid, oid);
}

// A server event or Suggestion snapshot names the Action's Suggestion, which is not necessarily
// the window showing it: a conversation opened from History is a different window, and every run
// of the Action, follow-ups and replays included, still carries the Suggestion id. So these binds
// take the Action only from no window, never from one that is open.
function adoptActionAssociation(actionId, overlayId) {
  const aid = normalizeId(actionId);
  const oid = normalizeId(overlayId);
  if (!aid || !oid) return;
  const current = actionToOverlayId.get(aid);
  if (current && current !== oid && hasOverlayWindow(current)) return;
  actionToOverlayId.set(aid, oid);
}

function cleanupMappingsForSuggestion(suggestionId) {
  const sid = normalizeId(suggestionId);
  if (!sid) return;
  for (const [pid, mappedSid] of [...processToSuggestion.entries()]) {
    if (mappedSid === sid) {
      processToSuggestion.delete(pid);
    }
  }
  for (const [aid, mappedOverlayId] of [...actionToOverlayId.entries()]) {
    if (mappedOverlayId === sid) {
      actionToOverlayId.delete(aid);
    }
  }
}

function cleanupMappingsForProcess(processId) {
  const pid = normalizeId(processId);
  if (pid) {
    processToSuggestion.delete(pid);
  }
}

function cleanupMappingsForAction(actionId) {
  const aid = normalizeId(actionId);
  if (aid) {
    actionToOverlayId.delete(aid);
  }
}

function clearActionAssociations() {
  actionToOverlayId.clear();
}

function resolveOverlayId({ suggestionId, processId, actionId }) {
  const sid = normalizeId(suggestionId);
  const pid = normalizeId(processId);
  const aid = normalizeId(actionId);

  if (sid && overlayWindows.has(sid)) {
    return sid;
  }
  if (aid && actionToOverlayId.has(aid)) {
    return actionToOverlayId.get(aid);
  }
  if (pid && processToSuggestion.has(pid)) {
    return processToSuggestion.get(pid);
  }
  return null;
}

function createMappedOverlayWindow(id, options) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  if (options.history) historyOverlayIds.add(id);
  if (options.conversation) conversationOverlayIds.add(id);
  const placement = stackPlacement(options.placementKind);
  const win = overlayWindowFactory.createConversationOverlayWindow({
    actionId: options.actionId ?? null,
    cell: placement.cell,
    entryMode: options.entryMode,
    stackIndex: placement.stackIndex,
    interactive: options.interactive,
    onDidFinishLoad: () => {
      if (overlayWindows.get(id) !== win || win.isDestroyed()) return;
      deliverOverlayState(id, win, runtime);
    },
    onReadyToShow: () => {
      if (overlayWindows.get(id) !== win || win.isDestroyed()) return;
      runtime.readyToShow = true;
      options.onReadyToShow(win, runtime);
    },
    onClosed: () => {
      overlayDragController.clearForWindow(win);
      overlayActivationTracker.unregisterOverlayWindow(win);
      if (overlayWindows.get(id) !== win) return;
      overlayWindows.delete(id);
      if (lastOverlayId === id) lastOverlayId = null;
      overlayState.delete(id);
      conversationOverlayIds.delete(id);
      cleanupMappingsForSuggestion(id);
      if (options.history) {
        historyOverlayIds.delete(id);
        syncMainWindowFocusableState();
      }
    },
  });
  overlayWindows.set(id, win);
  overlayWindowSlots.set(win, placement);
  overlayActivationTracker.registerOverlayWindow(win);
  lastOverlayId = id;
  if (options.history) syncMainWindowFocusableState();
  return win;
}

function createHistoryOverlayWindow(id) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  runtime.shellMode = INTERACTIVE_SHELL_MODE;
  return createMappedOverlayWindow(id, {
    history: true,
    interactive: true,
    placementKind: 'history',
    onReadyToShow: (win) => showInteractiveOverlayWindow(win, { visibleOnAllWorkspaces: true }),
  });
}

function createStandaloneConversationOverlayWindow(id, actionId) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  runtime.shellMode = INTERACTIVE_SHELL_MODE;
  return createMappedOverlayWindow(id, {
    actionId,
    conversation: true,
    entryMode: 'standalone',
    interactive: true,
    placementKind: standalonePlacementKind(actionId),
    onReadyToShow: showInteractiveOverlayWindow,
  });
}

function getOrCreateOverlayWindow(id) {
  const win = overlayWindows.get(id);
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  if (win && !win.isDestroyed()) {
    applyOverlayShellMode(win, runtime.shellMode);
    return win;
  }
  return createMappedOverlayWindow(id, {
    interactive: false,
    placementKind: 'suggestion',
    onReadyToShow: (overlay, state) => applyOverlayShellMode(overlay, state.shellMode),
  });
}

function getOrCreateHistoryOverlayWindow(id) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  runtime.shellMode = INTERACTIVE_SHELL_MODE;
  historyOverlayIds.add(id);
  syncMainWindowFocusableState();
  const win = overlayWindows.get(id);
  if (win && !win.isDestroyed()) {
    placeHiddenOverlayWindow(win, 'history');
    showInteractiveOverlayWindow(win);
    return win;
  }
  return createHistoryOverlayWindow(id);
}

function openStandaloneConversationOverlay(id, actionId = null) {
  readOwnerScope();
  const normalizedId = normalizeId(id);
  if (!normalizedId) throw new Error('Standalone Overlay ID is required.');
  const runtime = getOverlayRuntimeState(normalizedId);
  if (!runtime) throw new Error('Standalone Overlay state is unavailable.');
  const existing = overlayWindows.get(normalizedId);
  if (existing && !existing.isDestroyed()) {
    if (!runtime.ready || !runtime.readyToShow) return 'loading';
    placeHiddenOverlayWindow(existing, standalonePlacementKind(actionId));
    showInteractiveOverlayWindow(existing, { explicitFocus: true });
    existing.webContents.send('overlay:focusComposer');
    return 'focused';
  }
  createStandaloneConversationOverlayWindow(normalizedId, normalizeId(actionId));
  return 'created';
}

function hasOverlayWindow(id) {
  const normalizedId = normalizeId(id);
  const win = normalizedId ? overlayWindows.get(normalizedId) : null;
  return Boolean(win && !win.isDestroyed());
}

function destroyOverlayWindow(id) {
  const normalizedId = normalizeId(id);
  const win = normalizedId ? overlayWindows.get(normalizedId) : null;
  if (win && !win.isDestroyed()) win.destroy();
}

function showNotification(id) {
  readOwnerScope();
  const win = getOrCreateOverlayWindow(id);
  if (win && !win.isVisible()) {
    applyOverlayShellMode(win, PASSIVE_SHELL_MODE);
  }
}

function hideNotification(id) {
  const win = overlayWindows.get(id);
  if (win && !win.isDestroyed()) {
    // A conversation window is opened per conversation, so hiding it would leave
    // a renderer alive for the rest of the session; destroying it also releases
    // the Action association through onClosed.
    if (conversationOverlayIds.has(id)) {
      try {
        win.destroy();
      } catch {}
      return;
    }
    try {
      win.hide();
    } catch {}
    if (historyOverlayIds.has(id)) {
      historyOverlayIds.delete(id);
      syncMainWindowFocusableState();
    }
  }
}

function sendToAllOverlays(channel, payload) {
  for (const [, win] of overlayWindows) {
    if (win && !win.isDestroyed()) {
      try {
        win.webContents.send(channel, payload);
      } catch {}
    }
  }
}

function sendResetToAllOverlays(channel, payload) {
  for (const [id, runtime] of overlayState) {
    runtime.queue = runtime.queue.filter((message) => message.channel !== channel);
    enqueueOverlayMessage(id, channel, payload);
  }
}

function sendToOverlay(id, channel, payload) {
  const normalizedId = normalizeId(id);
  if (!normalizedId) return;
  enqueueOverlayMessage(normalizedId, channel, payload);
}

function setOverlaySnapshot(id, payload) {
  const normalizedId = normalizeId(id);
  if (!normalizedId || !payload || typeof payload !== 'object') return;
  overlaySnapshotPayloads.set(normalizedId, payload);
  adoptActionAssociation(payload.snapshot?.actionId, normalizedId);
  const runtime = getOverlayRuntimeState(normalizedId);
  if (!runtime) return;
  const win = overlayWindows.get(normalizedId);
  if (runtime.ready && win && !win.isDestroyed()) {
    try {
      win.webContents.send('overlay:snapshot', payload);
    } catch {}
  }
}

function dispatchEventToOverlay(channel, payload) {
  if (!payload || typeof payload !== 'object') return false;
  const meta = payload.meta || {};
  const data = payload.data || {};
  const targetId = resolveOverlayId({
    suggestionId: meta.suggestion_id || data.suggestion_id,
    processId: meta.process_id || data.process_id,
    actionId: meta.action_id || data.action_id,
  });
  if (!targetId) return false;
  sendToOverlay(targetId, channel, payload);
  return true;
}

const createNotificationIpcHandlers = createNotificationIpcHandlerFactory({
  BrowserWindow,
  screen,
  windows: {
    findOverlayIdByWindow,
    readOwnerScope,
    isOwnerScopeCurrent,
    getLastOverlayId: () => lastOverlayId,
    getOverlay: (id) => overlayWindows.get(id) || null,
    hide: hideNotification,
    normalizeId,
    setMainWindowGetter: (getter) => {
      getMainWindowForOverlayIsolation = getter;
    },
    setSnapshot: setOverlaySnapshot,
    showHistory: getOrCreateHistoryOverlayWindow,
  },
  interactions: {
    activationTracker: overlayActivationTracker,
    dragController: overlayDragController,
  },
});

module.exports = {
  setLocalOwnerIdGetter,
  clearForOwnerChange,
  showNotification,
  createNotificationIpcHandlers,
  sendToAllOverlays,
  sendResetToAllOverlays,
  hideOverlay: hideNotification,
  sendToOverlay,
  setOverlaySnapshot,
  registerProcessAssociation,
  registerActionAssociation,
  adoptActionAssociation,
  cleanupMappingsForProcess,
  cleanupMappingsForAction,
  clearActionAssociations,
  resolveOverlayId,
  resolveOverlayIdForSender,
  openStandaloneConversationOverlay,
  hasOverlayWindow,
  destroyOverlayWindow,
  dispatchEventToOverlay,
  setActionLiveSnapshotGetter,
  setOverlayPlacementGetter,
  setUiLanguageGetter,
  configureIpcWindowSecurity: auxiliaryWindowIpcSecurity.configure,
  isVisibleOverlayAtPoint: (point) => overlayActivationTracker.isVisibleOverlayAtPoint(point),
  hasRecentOverlayInteraction: (referenceMs) =>
    overlayActivationTracker.hasRecentInteraction(referenceMs),
};
