import type { App, BrowserWindow } from 'electron';
import type { AuthCallbackPayload, AuthCallbackResult } from '../auth/authCoordinator';
import { parseWorkRefUrl, type WorkRefKind } from '../history/workRefLink';

export const APP_PROTOCOL = 'pantaray';

type LinkedItem = { kind: WorkRefKind; id: string };

export type DeepLinkManager = {
  installAppHandlers: () => void;
  onMainWindowCreated: () => void;
  /** Startup is done: the main window exists and can show an item. */
  handleStartupArgs: (argv: unknown[]) => void;
};

function parseAuthDeepLinkUrl(urlStr: string): { attemptId: string; exchangeCode: string } | null {
  try {
    const url = new URL(urlStr);
    if (url.protocol !== `${APP_PROTOCOL}:`) return null;
    const isAuth = url.hostname === 'auth' || url.pathname === '/auth';
    if (!isAuth) return null;
    const attemptId = (url.searchParams.get('attempt_id') || '').trim();
    const exchangeCode = (url.searchParams.get('exchange_code') || '').trim();
    if (!attemptId || !exchangeCode) return null;
    return { attemptId, exchangeCode };
  } catch {
    return null;
  }
}

/**
 * `pantaray://` links opened from other apps: the login callback, and a task's or a suggestion's
 * link ("Copy link"), which shows that item in the main window. Any other link only brings the app
 * forward.
 */
export function createDeepLinkManager(params: {
  app: App;
  getMainWindow: () => BrowserWindow | null;
  handleCallback: (payload: AuthCallbackPayload) => AuthCallbackResult;
  showItem: (item: LinkedItem) => Promise<void>;
}): DeepLinkManager {
  // A link that launches the app on macOS arrives before the main window exists; the latest one
  // waits for startup to finish.
  let startupDone = false;
  let pendingItem: LinkedItem | null = null;

  function focusMainWindow(): void {
    try {
      const win = params.getMainWindow();
      if (win && !win.isDestroyed()) {
        if (!win.isVisible()) win.show();
        win.focus();
      }
    } catch {
      // no-op
    }
  }

  function showItem(item: LinkedItem): void {
    params.showItem(item).catch((error: unknown) => {
      console.warn('[deep-link] the linked item could not be shown:', error);
    });
  }

  function handleDeepLink(urlStr: unknown): void {
    const url = String(urlStr || '');
    const payload = parseAuthDeepLinkUrl(url);
    if (payload) {
      console.info('[deep-link] received auth deep link (code redacted)');
      const result = params.handleCallback({
        transport: 'deep_link',
        attemptId: payload.attemptId,
        exchangeCode: payload.exchangeCode,
      });
      if (!result.ok) {
        console.warn('[deep-link] auth callback rejected:', result.message);
      }
      focusMainWindow();
      return;
    }
    const item = parseWorkRefUrl(url);
    if (!item) {
      console.info('[deep-link] ignored (not an auth or item deep link)');
      return;
    }
    if (startupDone) showItem(item);
    else pendingItem = item;
  }

  function findDeepLinkArg(argv: unknown[]): string | undefined {
    return (argv || []).find(
      (arg): arg is string => typeof arg === 'string' && arg.startsWith(`${APP_PROTOCOL}://`)
    );
  }

  function installAppHandlers(): void {
    const app = params.app;
    const gotTheLock = app.requestSingleInstanceLock();
    if (!gotTheLock) {
      app.quit();
      return;
    }

    app.on('second-instance', (_event, commandLine) => {
      try {
        const deepLinkArg = findDeepLinkArg(commandLine);
        if (deepLinkArg) handleDeepLink(deepLinkArg);
      } catch {
        // no-op
      }
      focusMainWindow();
    });

    app.on('open-url', (event, urlStr) => {
      try {
        event.preventDefault();
      } catch {
        // no-op
      }
      console.info('[deep-link] open-url event received');
      handleDeepLink(urlStr);
    });
  }

  function handleStartupArgs(argv: unknown[]): void {
    startupDone = true;
    if (pendingItem) {
      showItem(pendingItem);
      pendingItem = null;
    }
    try {
      const arg = findDeepLinkArg(argv);
      if (arg) handleDeepLink(arg);
    } catch {
      // no-op
    }
  }

  return {
    installAppHandlers,
    onMainWindowCreated: focusMainWindow,
    handleStartupArgs,
  };
}
