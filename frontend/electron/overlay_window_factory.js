const { BrowserWindow, app, screen } = require('electron');
const path = require('path');
const { buildFrontendDevPageUrl } = require('./dev_frontend_env');
const { buildUiLanguageAdditionalArguments } = require('./ui_language_bootstrap');
const { MAIN_WINDOW_BACKGROUND_COLOR, MAIN_WINDOW_DEFAULT_SIZE } = require('./window_lifecycle');
const {
  OVERLAY_INITIAL_HEIGHT_PX,
  OVERLAY_WIDTH_PX,
  resolveConversationWindowBounds,
  resolveOverlayPlacement,
} = require('./dist/windows/overlayPlacement');

const OVERLAY_ALWAYS_ON_TOP_LEVEL = 'screen-saver';
// Centers the traffic lights on the window surface's 40 px header.
const CONVERSATION_WINDOW_TRAFFIC_LIGHT_POSITION = { x: 10, y: 14 };

// What each overlay window is: the small floating 'panel', or an ordinary 'window' that keeps
// none of the panel's shell (always-on-top, content-sized height, pointer drag).
const overlaySurfaces = new WeakMap();

function isConversationWindow(win) {
  return overlaySurfaces.get(win) === 'window';
}

function isOverlayPanel(win) {
  return overlaySurfaces.get(win) === 'panel';
}

function isDevRuntime() {
  return process.env.NODE_ENV === 'development' || !app.isPackaged;
}

function safeShowInactive(win) {
  if (!win || (typeof win.isDestroyed === 'function' && win.isDestroyed())) return;
  if (typeof win.showInactive === 'function') {
    win.showInactive();
  } else {
    win.show();
  }
}

function applyOverlayShellMode(win, shellMode) {
  if (win.isDestroyed() || isConversationWindow(win)) return;
  win.setFocusable(true);
  win.setAlwaysOnTop(true, OVERLAY_ALWAYS_ON_TOP_LEVEL);
  if (process.platform !== 'darwin' && shellMode === 'interactive') {
    win.show();
    win.focus();
  } else {
    safeShowInactive(win);
  }
  if (process.platform === 'darwin') win.moveTop();
}

function showInteractiveOverlayWindow(win, options = {}) {
  // A macOS panel can become the key window without activating Pantaray. If
  // loading finished after the user switched apps (no Pantaray window has focus),
  // show it without stealing input.
  const shouldFocus =
    process.platform !== 'darwin' ||
    options.explicitFocus === true ||
    BrowserWindow.getFocusedWindow() !== null;
  try {
    win.setFocusable(true);
  } catch {}
  try {
    if (shouldFocus) win.show();
    else safeShowInactive(win);
  } catch {}
  if (shouldFocus) {
    try {
      win.focus();
    } catch {}
  }
  try {
    win.setAlwaysOnTop(true, OVERLAY_ALWAYS_ON_TOP_LEVEL);
  } catch {}
  try {
    if (process.platform === 'darwin') win.moveTop();
  } catch {}
}

function hardDisableDevTools(win) {
  if (!win || isDevRuntime()) return;
  try {
    win.webContents.on('devtools-opened', () => {
      try {
        win.webContents.closeDevTools();
      } catch {}
    });
    win.webContents.on('before-input-event', (event, input) => {
      const key = String(input && input.key ? input.key : '');
      const ctrlOrMeta = Boolean(input && (input.control || input.meta));
      const shift = Boolean(input && input.shift);
      const alt = Boolean(input && input.alt);
      const isDevToolsShortcut =
        key === 'F12' ||
        (ctrlOrMeta && alt && key.toLowerCase() === 'i') ||
        (ctrlOrMeta && shift && key.toLowerCase() === 'i');
      if (isDevToolsShortcut) {
        try {
          event.preventDefault();
        } catch {}
      }
    });
  } catch {
    // no-op
  }
}

// The vertical edge a window keeps while its content grows (see resolveOverlayPlacement):
// - center: a middle-row window keeps its center while its first content loads in. The user's
//   first key or click returns it to growing downward from its top, so the composer and the
//   text being read stay where they are.
// - bottom: a bottom-row window keeps its current bottom edge, so it grows upward on screen.
const overlayVerticalAnchors = new WeakMap();

function getOverlayVerticalAnchor(win) {
  return overlayVerticalAnchors.get(win) ?? null;
}

function releaseOverlayCenter(win) {
  if (overlayVerticalAnchors.get(win)?.kind === 'center') overlayVerticalAnchors.delete(win);
}

function setOverlayVerticalAnchor(win, { y, anchor }) {
  overlayVerticalAnchors.delete(win);
  if (anchor === 'center') {
    overlayVerticalAnchors.set(win, { kind: 'center', y: y + OVERLAY_INITIAL_HEIGHT_PX / 2 });
    win.webContents.once('before-input-event', () => releaseOverlayCenter(win));
  } else if (anchor === 'bottom') {
    overlayVerticalAnchors.set(win, { kind: 'bottom' });
  }
}

function resolvePrimaryPlacement(cell, stackIndex) {
  return resolveOverlayPlacement(screen.getPrimaryDisplay().workArea, cell, stackIndex);
}

/**
 * Moves a hidden window that is shown again for another kind (a closed Suggestion reopened
 * from History) to that kind's cell. It keeps its content's height, placed by the cell's
 * anchor; the caller clamps it to the screen through the resize path.
 */
function moveOverlayWindowToCell(win, cell, stackIndex) {
  const placement = resolvePrimaryPlacement(cell, stackIndex);
  const { height } = win.getBounds();
  const y =
    placement.anchor === 'top'
      ? placement.y
      : placement.anchor === 'center'
        ? Math.round(placement.y + (OVERLAY_INITIAL_HEIGHT_PX - height) / 2)
        : placement.y + OVERLAY_INITIAL_HEIGHT_PX - height;
  win.setBounds({ x: placement.x, y });
  setOverlayVerticalAnchor(win, placement);
}

/**
 * An ordinary conversation window takes the main window's size, on the main window's display;
 * with no main window, the main window's default size on the primary display.
 */
function resolveConversationWindowPlacement(mainWindow) {
  const hasMainWindow = Boolean(mainWindow && !mainWindow.isDestroyed());
  const display = hasMainWindow
    ? screen.getDisplayMatching(mainWindow.getBounds())
    : screen.getPrimaryDisplay();
  const size = hasMainWindow ? mainWindow.getNormalBounds() : MAIN_WINDOW_DEFAULT_SIZE;
  return resolveConversationWindowBounds(display.workArea, size);
}

function createOverlayWindowFactory({ getUiLanguage, registerWindow }) {
  if (typeof getUiLanguage !== 'function') {
    throw new TypeError('Overlay window factory requires getUiLanguage.');
  }
  if (typeof registerWindow !== 'function') {
    throw new TypeError('Overlay window factory requires registerWindow.');
  }

  function webPreferences() {
    return {
      preload: path.join(__dirname, 'dist', 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      additionalArguments: buildUiLanguageAdditionalArguments(getUiLanguage()),
      devTools: isDevRuntime(),
    };
  }

  // Both surfaces load the same page and act on the same conversations, so both are trusted
  // as the 'overlay' IPC role.
  function attachOverlayPage(win, query, { onClosed, onDidFinishLoad, onReadyToShow }) {
    registerWindow(win);
    const notificationUrl = isDevRuntime()
      ? buildFrontendDevPageUrl('/notification.html')
      : `file://${path.join(__dirname, '../dist/notification.html')}`;
    const url = new URL(notificationUrl);
    for (const [name, value] of Object.entries(query)) {
      if (value) url.searchParams.set(name, value);
    }
    hardDisableDevTools(win);
    win.loadURL(url.toString());
    win.webContents.on('did-finish-load', () => onDidFinishLoad(win));
    win.once('ready-to-show', () => onReadyToShow(win));
    win.on('closed', () => onClosed(win));
  }

  function createConversationOverlayWindow({
    cell,
    entryMode,
    stackIndex,
    interactive,
    ...events
  }) {
    const placement = resolvePrimaryPlacement(cell, stackIndex);
    const win = new BrowserWindow({
      width: OVERLAY_WIDTH_PX,
      height: OVERLAY_INITIAL_HEIGHT_PX,
      x: placement.x,
      y: placement.y,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      alwaysOnTop: true,
      level: OVERLAY_ALWAYS_ON_TOP_LEVEL,
      skipTaskbar: true,
      resizable: false,
      movable: true,
      hasShadow: false,
      webPreferences: webPreferences(),
      show: false,
      ...(!interactive && { focusable: true }),
      ...(process.platform === 'darwin' && {
        type: 'panel',
        // A frameless window with rounded corners keeps an invisible title bar, and a
        // click in that top strip activates the app despite the non-activating panel.
        // The card draws its own corners on the transparent window.
        roundedCorners: false,
        fullscreenable: false,
        ...(interactive && { focusable: true }),
        acceptFirstMouse: true,
      }),
    });
    overlaySurfaces.set(win, 'panel');
    setOverlayVerticalAnchor(win, placement);
    attachOverlayPage(win, { mode: entryMode === 'standalone' ? 'standalone' : null }, events);
    return win;
  }

  /**
   * An ordinary window for a conversation that has work: titled, resizable, in the window
   * cycle and Mission Control, and behind other apps once they are clicked.
   */
  function createConversationWindow({ bounds, actionId, ...events }) {
    const win = new BrowserWindow({
      ...bounds,
      frame: true,
      backgroundColor: MAIN_WINDOW_BACKGROUND_COLOR,
      resizable: true,
      movable: true,
      fullscreenable: true,
      // The click that brings it back in front also acts, as on the main window.
      acceptFirstMouse: true,
      webPreferences: webPreferences(),
      show: false,
      ...(process.platform === 'darwin' && {
        titleBarStyle: 'hiddenInset',
        trafficLightPosition: CONVERSATION_WINDOW_TRAFFIC_LIGHT_POSITION,
      }),
    });
    overlaySurfaces.set(win, 'window');
    attachOverlayPage(win, { mode: 'standalone', actionId, surface: 'window' }, events);
    return win;
  }

  return {
    createConversationOverlayWindow,
    createConversationWindow,
  };
}

module.exports = {
  createOverlayWindowFactory,
  applyOverlayShellMode,
  getOverlayVerticalAnchor,
  isConversationWindow,
  isOverlayPanel,
  moveOverlayWindowToCell,
  releaseOverlayCenter,
  resolveConversationWindowPlacement,
  showInteractiveOverlayWindow,
};
