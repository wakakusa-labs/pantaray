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
  isConversationWindow,
  moveOverlayWindowToCell,
  resolveConversationWindowPlacement,
  showInteractiveOverlayWindow,
} = require('./overlay_window_factory');
const { normalizeId, createOverlayAssociations } = require('./overlay_associations');
const { restoreAndFocusWindow } = require('./dist/windows/windowVisibility');

const overlayWindows = new Map(); // key: suggestionId, value: BrowserWindow
let lastOverlayId = null;
const overlayState = new Map(); // id -> { ready, queue, shellMode }
const overlaySnapshotPayloads = new Map(); // id -> latest overlay:snapshot payload
// Overlay ids whose window this app opened for one conversation (a New-task panel or an
// ordinary conversation window). Their close control destroys the window instead of hiding it,
// so a session cannot accumulate invisible conversation renderers.
const conversationOverlayIds = new Set();
let getUiLanguage = null;
let getOverlayPlacements = null;
let getMainWindow = null;
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
const {
  registerProcessAssociation,
  registerActionAssociation,
  adoptActionAssociation,
  cleanupMappingsForSuggestion,
  cleanupMappingsForProcess,
  releaseActionAssociationWithoutWindow,
  clearActionAssociations,
  clearAllAssociations,
  resolveOverlayId,
  dispatchEventToOverlay,
} = createOverlayAssociations({ overlayWindows, hasOverlayWindow, sendToOverlay });
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

function setMainWindowGetter(getter) {
  if (typeof getter !== 'function') {
    throw new Error('setMainWindowGetter requires a function.');
  }
  getMainWindow = getter;
}

function currentMainWindow() {
  if (!getMainWindow) {
    throw new Error('Main window getter is required before creating conversation windows.');
  }
  return getMainWindow();
}

function overlayCellFor(placementKind) {
  if (!getOverlayPlacements) {
    throw new Error('Overlay placement getter is required before creating overlay windows.');
  }
  return getOverlayPlacements()[placementKind];
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
  clearAllAssociations();
  conversationOverlayIds.clear();
  lastOverlayId = null;
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

// The registry's side of a window's lifecycle. Every callback first checks that the window is
// still the one registered for its id: a window replaced under the same id (a panel swapped for
// an ordinary window, or a window recreated after an owner change) must neither deliver state
// nor release the associations its successor now holds.
function overlayWindowEvents(id, runtime, onReadyToShow) {
  return {
    onDidFinishLoad: (win) => {
      if (overlayWindows.get(id) !== win || win.isDestroyed()) return;
      deliverOverlayState(id, win, runtime);
    },
    onReadyToShow: (win) => {
      if (overlayWindows.get(id) !== win || win.isDestroyed()) return;
      runtime.readyToShow = true;
      onReadyToShow(win, runtime);
    },
    onClosed: (win) => {
      overlayDragController.clearForWindow(win);
      overlayActivationTracker.unregisterOverlayWindow(win);
      if (overlayWindows.get(id) !== win) return;
      overlayWindows.delete(id);
      if (lastOverlayId === id) lastOverlayId = null;
      overlayState.delete(id);
      conversationOverlayIds.delete(id);
      cleanupMappingsForSuggestion(id);
    },
  };
}

function createMappedOverlayWindow(id, options) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  if (options.conversation) conversationOverlayIds.add(id);
  const placement = stackPlacement(options.placementKind);
  const win = overlayWindowFactory.createConversationOverlayWindow({
    cell: placement.cell,
    entryMode: options.entryMode,
    stackIndex: placement.stackIndex,
    interactive: options.interactive,
    ...overlayWindowEvents(id, runtime, options.onReadyToShow),
  });
  overlayWindows.set(id, win);
  overlayWindowSlots.set(win, placement);
  overlayActivationTracker.registerOverlayWindow(win);
  lastOverlayId = id;
  return win;
}

// An ordinary window for an Action's conversation. It takes the id's registry entry, so the
// Action's associations and any queued state now reach it; it is not registered with the panels'
// activation tracker, whose pointer drag and click-through rules are a panel's.
function createConversationWindow(id, actionId) {
  const runtime = getOverlayRuntimeState(id);
  runtime.ready = false;
  runtime.readyToShow = false;
  conversationOverlayIds.add(id);
  const win = overlayWindowFactory.createConversationWindow({
    actionId,
    bounds: resolveConversationWindowPlacement(currentMainWindow()),
    ...overlayWindowEvents(id, runtime, (shown) => shown.show()),
  });
  overlayWindows.set(id, win);
  lastOverlayId = id;
  return win;
}

function createHistoryOverlayWindow(id) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  runtime.shellMode = INTERACTIVE_SHELL_MODE;
  return createMappedOverlayWindow(id, {
    interactive: true,
    placementKind: 'history',
    onReadyToShow: (win) => showInteractiveOverlayWindow(win),
  });
}

function createStandaloneConversationOverlayWindow(id) {
  const runtime = getOverlayRuntimeState(id);
  if (!runtime) return null;
  runtime.shellMode = INTERACTIVE_SHELL_MODE;
  return createMappedOverlayWindow(id, {
    conversation: true,
    entryMode: 'standalone',
    interactive: true,
    placementKind: 'started',
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
  const win = overlayWindows.get(id);
  if (win && !win.isDestroyed()) {
    placeHiddenOverlayWindow(win, 'history');
    showInteractiveOverlayWindow(win);
    return win;
  }
  return createHistoryOverlayWindow(id);
}

/**
 * Opens a conversation the user asked for. One with work (an Action) is an ordinary window; a
 * New task starts as a panel. An Action has at most one surface, so an existing one is focused
 * again: its window, or a visible panel showing it, whose unsent draft lives only in its page.
 * A hidden panel showing it (a Suggestion accepted elsewhere, then closed) is replaced by a
 * window under the same id, keeping the Action's associations.
 */
function openStandaloneConversationOverlay(id, actionId = null) {
  readOwnerScope();
  const normalizedId = normalizeId(id);
  if (!normalizedId) throw new Error('Standalone Overlay ID is required.');
  const normalizedActionId = normalizeId(actionId);
  const runtime = getOverlayRuntimeState(normalizedId);
  if (!runtime) throw new Error('Standalone Overlay state is unavailable.');
  const current = overlayWindows.get(normalizedId);
  const existing = current && !current.isDestroyed() ? current : null;
  const keepsSurface = existing && (isConversationWindow(existing) || existing.isVisible());
  if (normalizedActionId && !keepsSurface) {
    createConversationWindow(normalizedId, normalizedActionId);
    existing?.destroy();
    return 'created';
  }
  if (existing) {
    if (!runtime.ready || !runtime.readyToShow) return 'loading';
    if (isConversationWindow(existing)) {
      restoreAndFocusWindow(existing);
    } else {
      placeHiddenOverlayWindow(existing, 'started');
      showInteractiveOverlayWindow(existing, { explicitFocus: true });
    }
    existing.webContents.send('overlay:focusComposer');
    return 'focused';
  }
  createStandaloneConversationOverlayWindow(normalizedId);
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
  releaseActionAssociationWithoutWindow,
  clearActionAssociations,
  resolveOverlayId,
  resolveOverlayIdForSender,
  openStandaloneConversationOverlay,
  hasOverlayWindow,
  destroyOverlayWindow,
  dispatchEventToOverlay,
  setActionLiveSnapshotGetter,
  setMainWindowGetter,
  setOverlayPlacementGetter,
  setUiLanguageGetter,
  configureIpcWindowSecurity: auxiliaryWindowIpcSecurity.configure,
  isVisibleOverlayAtPoint: (point) => overlayActivationTracker.isVisibleOverlayAtPoint(point),
  hasRecentOverlayInteraction: (referenceMs) =>
    overlayActivationTracker.hasRecentInteraction(referenceMs),
};
