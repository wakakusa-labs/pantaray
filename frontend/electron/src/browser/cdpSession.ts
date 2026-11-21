/**
 * The CDP connection the browser engine's page code drives, whichever page it is: Electron's
 * `webContents.debugger` for the app's hidden page, or a tab of the user's browser
 * (`UserBrowserTab.cdp` in `userBrowserConnection`). Both use the flat-session shape: a command
 * or event of one of the page's frame or worker sessions names it by `sessionId`; one without
 * goes to, or comes from, the page itself. Electron's `Debugger` names the page's own events by
 * an empty `sessionId`, the user's browser by none: a listener reads it through `childSession`.
 */

import type { BrowserPageData } from './browserContract';

/** The frame or worker session an event came from; undefined for the page's own. */
export function childSession(sessionId: string | undefined): string | undefined {
  return sessionId === '' ? undefined : sessionId;
}

export type CdpSession = {
  sendCommand(method: string, params?: object, sessionId?: string): Promise<unknown>;
  /** `params` has the shape `method` defines; a listener narrows it by the methods it handles. */
  on(
    event: 'message',
    listener: (event: unknown, method: string, params: unknown, sessionId?: string) => void
  ): unknown;
};

/** The page an operation reads or acts on, with its console since the session opened. */
export type CdpPage = {
  cdp: CdpSession;
  console: { lines: NonNullable<BrowserPageData['console']>; cut: boolean };
};
