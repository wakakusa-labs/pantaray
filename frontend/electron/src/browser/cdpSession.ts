/**
 * The CDP connection the browser engine's page code drives, whichever page it is: Electron's
 * `webContents.debugger` for the app's hidden page, or a tab of the user's browser
 * (`UserBrowserTab.cdp` in `userBrowserConnection`). Both use the flat-session shape: a command
 * or event of one of the page's frame or worker sessions names it by `sessionId`; one without
 * goes to, or comes from, the page itself.
 */

import type { BrowserPageData } from './browserContract';

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
