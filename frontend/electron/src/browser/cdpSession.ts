/**
 * The CDP connection the browser engine's page code drives, whichever page it is: Electron's
 * `webContents.debugger` for the app's hidden page, or a WebSocket to the user's browser
 * (`userBrowserConnection`). Both use the flat-session shape: a command or event names the
 * target session it belongs to by `sessionId`; one without goes to, or comes from, what the
 * connection was opened on (the app's page, or the user's whole browser).
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
