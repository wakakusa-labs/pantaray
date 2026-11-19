import type { BrowserWindowConstructorOptions } from 'electron';

/** The page's viewport in CSS pixels; a viewport screenshot has this size. */
export const PAGE_VIEWPORT = { width: 1280, height: 800 } as const;

/**
 * The hidden window that holds one browser session's page. It is never shown or focused: input
 * reaches it only as CDP events through `webContents.debugger`, and it keeps painting while
 * hidden, so captures and prints see the page's current state.
 *
 * `partition` is the session's own in-memory partition (no `persist:` prefix), so nothing is
 * shared between sessions or with the app's own session.
 */
export function pageWindowOptions(partition: string): BrowserWindowConstructorOptions {
  return {
    show: false,
    paintWhenInitiallyHidden: true,
    skipTaskbar: true,
    focusable: false,
    useContentSize: true,
    width: PAGE_VIEWPORT.width,
    height: PAGE_VIEWPORT.height,
    webPreferences: {
      partition,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      // Runs the session's frame preload in every frame; Node stays off in the sandbox.
      nodeIntegrationInSubFrames: true,
      webviewTag: false,
      devTools: false,
      disableDialogs: true,
      webSecurity: true,
      backgroundThrottling: false,
      javascript: true,
    },
  };
}
