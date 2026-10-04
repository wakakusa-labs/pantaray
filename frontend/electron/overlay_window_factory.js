const { BrowserWindow, app, screen } = require('electron');
const path = require('path');
const { buildFrontendDevPageUrl } = require('./dev_frontend_env');
const { buildUiLanguageAdditionalArguments } = require('./ui_language_bootstrap');

const DEFAULT_OVERLAY_WIDTH_PX = 520;
const DEFAULT_OVERLAY_HEIGHT_PX = 120;
const DEFAULT_SCREEN_MARGIN_PX = 20;
const DEFAULT_OVERLAY_GAP_PX = 12;
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

function resolveOverlayPosition(index) {
  const primaryDisplay = screen.getPrimaryDisplay();
  const bounds = primaryDisplay.bounds;
  const bx = Number(bounds?.x || 0);
  const by = Number(bounds?.y || 0);
  const maxRows = Math.max(
    1,
    Math.floor(
      (bounds.height - DEFAULT_SCREEN_MARGIN_PX * 2) /
        (DEFAULT_OVERLAY_HEIGHT_PX + DEFAULT_OVERLAY_GAP_PX)
    )
  );
  const row = Math.min(index, maxRows - 1);
  return {
    x: bx + bounds.width - DEFAULT_OVERLAY_WIDTH_PX - DEFAULT_SCREEN_MARGIN_PX,
    y: by + DEFAULT_SCREEN_MARGIN_PX + row * (DEFAULT_OVERLAY_HEIGHT_PX + DEFAULT_OVERLAY_GAP_PX),
  };
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
    entryMode,
    index,
    interactive,
    onClosed,
    onDidFinishLoad,
    onReadyToShow,
  }) {
    const position = resolveOverlayPosition(index);
    const win = new BrowserWindow({
      width: DEFAULT_OVERLAY_WIDTH_PX,
      height: DEFAULT_OVERLAY_HEIGHT_PX,
      ...position,
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
        fullscreenable: false,
        ...(interactive && { focusable: true }),
        acceptFirstMouse: true,
      }),
    });
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
  showInteractiveOverlayWindow,
};
