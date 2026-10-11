import type { App, WebContents } from 'electron';

type LoggerLike = { warn?: (name: string, payload?: unknown) => void };

function isWebUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url);
    return protocol === 'https:' || protocol === 'http:';
  } catch {
    return false;
  }
}

/**
 * A link a page opens in a new window (`target="_blank"`, `window.open`) goes to the default
 * browser. The app never creates a window for it: an Electron window has no address bar and none
 * of the user's sign-ins, and web content does not belong inside Pantaray.
 */
export function openNewWindowsInDefaultBrowser(params: {
  app: App;
  openExternal: (url: string) => Promise<void>;
  logger: LoggerLike | null;
}): void {
  params.app.on('web-contents-created', (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      if (isWebUrl(url)) {
        params.openExternal(url).catch((error: unknown) => {
          params.logger?.warn?.('WINDOW_OPEN_EXTERNAL_ERR', { err: error });
        });
      }
      return { action: 'deny' };
    });
  });
}

/** The document a URL loads: everything but the hash, which the app routes by. */
function documentOf(url: string): string | null {
  try {
    const parsed = new URL(url);
    parsed.hash = '';
    return parsed.href;
  } catch {
    return null;
  }
}

/**
 * Keeps a window on the document main loaded. A link in its content that would load another
 * one is dropped: a relative link resolves against the app's own address and would replace
 * the app with whatever that address serves. Main's own loads, reloads and hash routes stay
 * on the same document (main's loadURL and loadFile emit no will-navigate at all).
 */
export function keepWindowOnItsDocument(contents: Pick<WebContents, 'on' | 'getURL'>): void {
  contents.on('will-navigate', (event, url) => {
    const current = documentOf(contents.getURL());
    if (current === null || documentOf(url) !== current) event.preventDefault();
  });
}
