const { BrowserWindow, app, screen } = require('electron');
const path = require('path');
const { buildFrontendDevPageUrl } = require('./dev_frontend_env');
const { buildUiLanguageAdditionalArguments } = require('./ui_language_bootstrap');
const {
  OVERLAY_INITIAL_HEIGHT_PX,
  OVERLAY_WIDTH_PX,
  resolveOverlayPlacement,
} = require('./dist/windows/overlayPlacement');

const OVERLAY_ALWAYS_ON_TOP_LEVEL = 'screen-saver';

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
  if (win.isDestroyed()) return;
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
  try {
    if (
      options.visibleOnAllWorkspaces === true &&
      typeof win.setVisibleOnAllWorkspaces === 'function'
    ) {
      win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    }
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
function moveOverlayWindowToCell(win, cell) {
  const placement = resolvePrimaryPlacement(cell, 0);
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

function createOverlayWindowFactory({ getUiLanguage, registerWindow }) {
  if (typeof getUiLanguage !== 'function') {
    throw new TypeError('Overlay window factory requires getUiLanguage.');
  }
  if (typeof registerWindow !== 'function') {
    throw new TypeError('Overlay window factory requires registerWindow.');
  }

  function additionalArguments() {
    return buildUiLanguageAdditionalArguments(getUiLanguage());
  }

  function loadOverlayPage(win, entryMode, actionId) {
    const notificationUrl = isDevRuntime()
      ? buildFrontendDevPageUrl('/notification.html')
      : `file://${path.join(__dirname, '../dist/notification.html')}`;
    const url = new URL(notificationUrl);
    if (entryMode === 'standalone') url.searchParams.set('mode', 'standalone');
    if (actionId) url.searchParams.set('actionId', actionId);
    hardDisableDevTools(win);
    win.loadURL(url.toString());
  }

  function createConversationOverlayWindow({
    actionId,
    cell,
    entryMode,
    stackIndex,
    interactive,
    onClosed,
    onDidFinishLoad,
    onReadyToShow,
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
      webPreferences: {
        preload: path.join(__dirname, 'dist', 'preload.js'),
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        additionalArguments: additionalArguments(),
        devTools: isDevRuntime(),
      },
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
    setOverlayVerticalAnchor(win, placement);
    registerWindow(win);
    loadOverlayPage(win, entryMode, actionId);
    win.webContents.on('did-finish-load', () => onDidFinishLoad(win));
    win.once('ready-to-show', () => onReadyToShow(win));
    win.on('closed', () => onClosed(win));
    return win;
  }

  return {
    createConversationOverlayWindow,
  };
}

module.exports = {
  createOverlayWindowFactory,
  applyOverlayShellMode,
  getOverlayVerticalAnchor,
  moveOverlayWindowToCell,
  releaseOverlayCenter,
  showInteractiveOverlayWindow,
};
