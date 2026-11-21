/**
 * Connecting to the user's own browser (Chrome 144+ auto-connect): Electron main finds the
 * running browser through its user-data-dir, checks that what listens on its debugging port is
 * that browser by code signature, and opens one WebSocket to it, which the browser holds until
 * the user allows it. While remote debugging is off it waits for the user to turn it on. A
 * refusal names the browser and tells the user only what they can act on.
 *
 * The socket reaches the whole browser, so it stays inside this module. Callers get only tabs
 * of their own: a tab handle sends page commands to its own sessions and hears only their
 * events. Nothing else about the browser, the user's tabs included, leaves this module.
 */

import type { ClientRequest } from 'node:http';
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

import type { UserBrowserId } from './browserContract';
import { BrowserOperationError, type BrowserFailureCode } from './browserSession';
import type { CdpSession } from './cdpSession';
import { KNOWN_BROWSERS, type KnownBrowser } from './knownBrowsers';

/** The part of the `ws` client this module uses; the package ships no types. */
type WebSocketClient = {
  on(event: 'open' | 'close', listener: () => void): void;
  on(event: 'message', listener: (data: Buffer) => void): void;
  on(event: 'error', listener: (error: Error) => void): void;
  on(
    event: 'unexpected-response',
    listener: (request: ClientRequest, response: { statusCode?: number }) => void
  ): void;
  send(data: string): void;
  close(): void;
  terminate(): void;
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const WebSocket = require('ws') as new (url: string) => WebSocketClient;

export type CommandResult = { exitCode: number; stdout: string };
/**
 * Runs a program without a shell and kills it when `signal` aborts. Rejects when it could not be
 * started, was killed, or ran past its time.
 */
export type CommandRunner = (
  file: string,
  args: readonly string[],
  signal: AbortSignal
) => Promise<CommandResult>;

/** lsof and codesign answer in well under a second; one stuck past this makes the browser unusable. */
const CHECK_TIMEOUT_MS = 5_000;
/** How often the browser is checked again while the user turns remote debugging on. */
const DEBUGGING_POLL_MS = 1_000;

/**
 * What the user is asked to do in their browser: turn remote debugging on, or allow Pantaray in
 * once it is on.
 */
export type UserBrowserWaitKind = 'debugging_off' | 'allow';
/** Told when a wait for the user starts; the closer it returns runs once that wait ends. */
export type UserBrowserWaitHook = (kind: UserBrowserWaitKind) => () => void;

export const runCommand: CommandRunner = (file, args, signal) =>
  new Promise((resolve, reject) => {
    execFile(
      file,
      args,
      { encoding: 'utf8', signal, timeout: CHECK_TIMEOUT_MS },
      (error, stdout) => {
        if (error === null) resolve({ exitCode: 0, stdout });
        else if (typeof error.code === 'number') resolve({ exitCode: error.code, stdout });
        else reject(error);
      }
    );
  });

/**
 * One hidden background tab of the caller's own. `cdp` sends to the tab's own session when no
 * `sessionId` is given, or to a frame or worker session the tab reported, and hears only those
 * sessions' events; the tab's own come without a `sessionId`.
 */
export type UserBrowserTab = { cdp: CdpSession; close(): Promise<void> };

export type UserBrowserConnection = {
  /**
   * `onPopup` hears the URL of each popup the tab opened, which is closed at once. The tab lives
   * under `signal`: once it aborts, opening stops (closing a target already created), nothing more
   * is sent for the tab but its close, and none of its events are heard.
   */
  openTab(onPopup: (url: string) => void, signal: AbortSignal): Promise<UserBrowserTab>;
  /** Settles once the socket has closed, whichever side closed it. */
  closed: Promise<void>;
  close(): void;
};

/** A command the browser refused, or one a tab does not send. */
export class CdpCommandError extends Error {}

const LOCK_FILE = 'SingletonLock';
const PORT_FILE = 'DevToolsActivePort';
/** Chromium's lock is a symlink to `<hostname>-<pid>`. */
const LOCK_TARGET = /-(\d+)$/;
const BROWSER_PATH = /^\/devtools\/browser\/[0-9A-Za-z-]+$/;
/** What macOS answers when privacy settings keep Pantaray out of the browser's files. */
const BLOCKED_ERRNO = new Set(['EPERM', 'EACCES']);

/**
 * What a tab sends: the page code's commands (snapshot, interactions, capture) and the tab's
 * guards. Chromium routes `Target` commands by the target they name whatever the session, and
 * auto-attach would reach the profile's service and shared workers, so no `Target` method is
 * here, nor anything that reaches the whole browser profile (`Browser`, `Storage`, cookies,
 * downloads). Add a method only when a tab needs it.
 */
const TAB_COMMANDS = new Set([
  'Accessibility.getFullAXTree',
  'Audits.enable',
  'DOM.describeNode',
  'DOM.focus',
  'DOM.getContentQuads',
  'DOM.getFrameOwner',
  'DOM.getNodeForLocation',
  'DOM.resolveNode',
  'DOM.scrollIntoViewIfNeeded',
  'Emulation.setDeviceMetricsOverride',
  'Emulation.setFocusEmulationEnabled',
  'Fetch.continueRequest',
  'Fetch.enable',
  'Fetch.failRequest',
  'Fetch.fulfillRequest',
  'Input.dispatchKeyEvent',
  'Input.dispatchMouseEvent',
  'Input.insertText',
  'Log.enable',
  'Network.enable',
  'Network.setBypassServiceWorker',
  'Page.addScriptToEvaluateOnNewDocument',
  'Page.captureScreenshot',
  'Page.createIsolatedWorld',
  'Page.enable',
  'Page.getFrameTree',
  'Page.getLayoutMetrics',
  'Page.getNavigationHistory',
  'Page.handleJavaScriptDialog',
  'Page.navigate',
  'Page.navigateToHistoryEntry',
  'Page.setInterceptFileChooserDialog',
  'Runtime.callFunctionOn',
  'Runtime.enable',
  'Runtime.evaluate',
  'Runtime.releaseObjectGroup',
  'Runtime.runIfWaitingForDebugger',
]);

/**
 * The children a tab's sessions attach: its frames and dedicated workers, each paused until the
 * caller has guarded it. A service or shared worker serves the whole profile's tabs of its
 * origin, so it is never attached; the first matching entry decides.
 */
const CHILD_TYPES = new Set(['iframe', 'worker']);
/** What a tab may open: the web or its blank start page; a browser page reaches other tabs. */
const opensWebOrBlank = (url: unknown): boolean =>
  url === 'about:blank' ||
  (typeof url === 'string' &&
    URL.canParse(url) &&
    ['http:', 'https:'].includes(new URL(url).protocol));
/** Hidden: never in the tab strip, and gone with the connection. */
const NEW_TAB = { url: 'about:blank', hidden: true, background: true };
const AUTO_ATTACH = {
  autoAttach: true,
  waitForDebuggerOnStart: true,
  flatten: true,
  filter: [{ type: 'iframe' }, { type: 'worker' }, { exclude: true }],
};

const refusal = (browser: KnownBrowser) => {
  const { name } = browser;
  const as = (code: BrowserFailureCode, message: string) => () =>
    new BrowserOperationError(code, message);
  return {
    notRunning: as('BROWSER_USER_NOT_RUNNING', `${name} is not running.`),
    fileBlocked: as(
      'BROWSER_USER_FILE_BLOCKED',
      `macOS did not let Pantaray read ${name}'s files; allow it in System Settings › ` +
        'Privacy & Security › Files & Folders.'
    ),
    denied: as('BROWSER_USER_DENIED', `The user did not allow Pantaray to use ${name}.`),
    unavailable: as('BROWSER_USER_UNAVAILABLE', `${name} could not be used.`),
  };
};
type Refusal = ReturnType<typeof refusal>;

const errnoOf = (error: unknown): unknown => (error as NodeJS.ErrnoException).code;

/** Whether a browser holds the user-data-dir now: its lock names a live process. */
async function browserRunning(userDataDir: string, refuse: Refusal): Promise<boolean> {
  let target: string;
  try {
    target = await fs.promises.readlink(path.join(userDataDir, LOCK_FILE));
  } catch (error) {
    if (errnoOf(error) === 'ENOENT') return false;
    throw BLOCKED_ERRNO.has(String(errnoOf(error))) ? refuse.fileBlocked() : refuse.unavailable();
  }
  const pid = Number(LOCK_TARGET.exec(target)?.[1]);
  if (!Number.isSafeInteger(pid) || pid <= 0) throw refuse.unavailable();
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM: the process exists but is not ours to signal.
    return errnoOf(error) !== 'ESRCH';
  }
}

/**
 * The browser's WebSocket endpoint as it wrote it: the port on line 1, the path on line 2. Null
 * while remote debugging is off, when the browser writes no file.
 */
async function endpointOf(
  userDataDir: string,
  refuse: Refusal
): Promise<{ port: number; path: string } | null> {
  let text: string;
  try {
    text = await fs.promises.readFile(path.join(userDataDir, PORT_FILE), 'utf8');
  } catch (error) {
    if (errnoOf(error) === 'ENOENT') return null;
    throw BLOCKED_ERRNO.has(String(errnoOf(error))) ? refuse.fileBlocked() : refuse.unavailable();
  }
  const [portLine = '', pathLine = ''] = text.split('\n').map((line) => line.trim());
  const port = /^\d{1,5}$/.test(portLine) ? Number(portLine) : 0;
  if (port < 1 || port > 65_535 || !BROWSER_PATH.test(pathLine)) throw refuse.unavailable();
  return { port, path: pathLine };
}

/** Runs a check; an abort ends the wait at once, whether or not the runner heeds it. */
async function run(
  runner: CommandRunner,
  file: string,
  args: readonly string[],
  signal: AbortSignal,
  refuse: Refusal
): Promise<CommandResult> {
  let onAbort: () => void = () => undefined;
  const aborted = new Promise<never>((_resolve, reject) => {
    onAbort = () => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
  });
  try {
    return await Promise.race([runner(file, args, signal), aborted]);
  } catch {
    if (signal.aborted) throw signal.reason;
    // The check could not start, or did not finish in time: nothing about the browser is known.
    throw refuse.unavailable();
  } finally {
    signal.removeEventListener('abort', onAbort);
  }
}

/**
 * Checks that the one process listening on the port is the browser or its helper, signed by its
 * team; false when nothing listens. A file left by a browser that quit can name a port another
 * process took since.
 *
 * Design limit: the process could change between this check and the connection; that needs the
 * browser to quit and another process to take its port within that moment.
 */
async function verifyListener(
  port: number,
  browser: KnownBrowser,
  runner: CommandRunner,
  signal: AbortSignal,
  refuse: Refusal
): Promise<boolean> {
  const check = (file: string, args: string[]) => run(runner, file, args, signal, refuse);
  const listing = await check('/usr/sbin/lsof', [
    '-nP',
    `-iTCP@127.0.0.1:${port}`,
    '-sTCP:LISTEN',
    '-Fp',
  ]);
  // lsof exits 1 when nothing matches.
  if (listing.exitCode !== 0 && listing.exitCode !== 1) throw refuse.unavailable();
  const pids = new Set(
    listing.stdout
      .split('\n')
      .filter((line) => /^p\d+$/.test(line))
      .map((line) => line.slice(1))
  );
  // Nothing listens once the user turned remote debugging off, though the file stays.
  if (pids.size === 0) return false;
  if (pids.size > 1) throw refuse.unavailable();
  const requirement =
    `=anchor apple generic and certificate leaf[subject.OU] = "${browser.teamId}" and ` +
    `(identifier "${browser.bundleId}" or identifier "${browser.bundleId}.helper")`;
  const verified = await check('/usr/bin/codesign', ['--verify', '-R', requirement, [...pids][0]]);
  if (verified.exitCode !== 0) throw refuse.unavailable();
  return true;
}

/**
 * The endpoint once remote debugging is on: the browser's file names a port it listens on. Until
 * then the debugging-off wait is up and the browser is checked again, with no time limit; `signal`
 * ends the wait.
 *
 * Design limit: polling reads the file (and runs lsof once the file is there) every
 * DEBUGGING_POLL_MS while the user turns debugging on; watch the file instead if that wait is
 * seen to cost noticeable CPU.
 */
async function endpointWhenOn(
  userDataDir: string,
  browser: KnownBrowser,
  runner: CommandRunner,
  waitForUser: UserBrowserWaitHook,
  signal: AbortSignal,
  refuse: Refusal
): Promise<{ port: number; path: string }> {
  let endWait: (() => void) | null = null;
  try {
    for (;;) {
      const endpoint = await endpointOf(userDataDir, refuse);
      signal.throwIfAborted();
      if (
        endpoint !== null &&
        (await verifyListener(endpoint.port, browser, runner, signal, refuse))
      ) {
        return endpoint;
      }
      endWait ??= waitForUser('debugging_off');
      // The timer goes with the abort, which is the only way it rejects.
      await sleep(DEBUGGING_POLL_MS, undefined, { signal }).catch(() => signal.throwIfAborted());
    }
  } finally {
    endWait?.();
  }
}

type CdpMessage = {
  id?: number;
  result?: unknown;
  error?: { message?: string };
  method?: string;
  params?: {
    sessionId?: string;
    targetInfo?: { targetId?: string; openerId?: string; url?: string; type?: string };
  };
  sessionId?: string;
};
type Listener = Parameters<CdpSession['on']>[1];
type Pending = { resolve: (value: unknown) => void; reject: (e: Error) => void };

type Tab = {
  targetId: string;
  sessionId: string;
  /** Frame and worker sessions the tab's own sessions reported attached. */
  children: Set<string>;
  listeners: Listener[];
  onPopup: (url: string) => void;
};

/** The browser on an open socket, reached only through the tabs it opens. */
function browserOver(socket: WebSocketClient): UserBrowserConnection {
  let nextId = 0;
  const pending = new Map<number, Pending>();
  /** Each tab by its own session id and each of its children's. */
  const bySession = new Map<string, Tab>();
  const byTarget = new Map<string, Tab>();
  let isClosed = false;
  let onClosed: () => void = () => undefined;
  const closed = new Promise<void>((resolve) => (onClosed = resolve));

  async function send(method: string, params: object, sessionId?: string): Promise<unknown> {
    // `ws` drops what is sent after the close, which would never be answered.
    if (isClosed) throw new CdpCommandError('The connection to the browser closed.');
    nextId += 1;
    const id = nextId;
    const result = new Promise<unknown>((resolve, reject) => pending.set(id, { resolve, reject }));
    socket.send(JSON.stringify({ id, method, params, sessionId }));
    return result;
  }

  function onTabEvent(tab: Tab, message: CdpMessage & { method: string; sessionId: string }) {
    const child = message.params?.sessionId;
    if (child !== undefined && message.method === 'Target.attachedToTarget') {
      if (!CHILD_TYPES.has(message.params?.targetInfo?.type ?? '')) {
        // Detaching also lets it run on; it never reaches the caller.
        send('Target.detachFromTarget', { sessionId: child }, message.sessionId).catch(
          () => undefined
        );
        return;
      }
      tab.children.add(child);
      bySession.set(child, tab);
      // Sent before the caller can resume it; if refused, nothing attaches under it.
      send('Target.setAutoAttach', AUTO_ATTACH, child).catch(() => undefined);
    } else if (child !== undefined && message.method === 'Target.detachedFromTarget') {
      if (!tab.children.delete(child)) return;
      bySession.delete(child);
    }
    const from = message.sessionId === tab.sessionId ? undefined : message.sessionId;
    for (const listener of tab.listeners) listener({}, message.method, message.params ?? {}, from);
  }

  /** The only browser-level event acted on: a popup one of the tabs opened, which is closed. */
  function onBrowserEvent(message: CdpMessage): void {
    const info = message.params?.targetInfo;
    if (message.method !== 'Target.targetCreated' || info?.openerId === undefined) return;
    const opener = byTarget.get(info.openerId);
    if (opener === undefined || info.targetId === undefined) return;
    opener.onPopup(info.url ?? '');
    // A popup that closed itself already is as good.
    send('Target.closeTarget', { targetId: info.targetId }).catch(() => undefined);
  }

  socket.on('message', (data) => {
    const message = JSON.parse(data.toString('utf8')) as CdpMessage;
    if (message.id !== undefined) {
      const call = pending.get(message.id);
      pending.delete(message.id);
      if (message.error !== undefined) {
        call?.reject(new CdpCommandError(message.error.message ?? 'The command failed.'));
      } else {
        call?.resolve(message.result ?? {});
      }
    } else if (message.method === undefined) {
      return;
    } else if (message.sessionId === undefined) {
      onBrowserEvent(message);
    } else {
      const tab = bySession.get(message.sessionId);
      if (tab !== undefined) {
        onTabEvent(tab, { ...message, method: message.method, sessionId: message.sessionId });
      }
    }
  });
  socket.on('close', () => {
    isClosed = true;
    for (const call of pending.values()) {
      call.reject(new CdpCommandError('The connection to the browser closed.'));
    }
    pending.clear();
    onClosed();
  });
  // Every error closes the socket, which the listener above handles.
  socket.on('error', () => undefined);

  async function openTab(
    onPopup: (url: string) => void,
    signal: AbortSignal
  ): Promise<UserBrowserTab> {
    /** Sends one step of opening the tab, none once the tab's signal aborted. */
    const step = async (method: string, params: object, sessionId?: string) => {
      signal.throwIfAborted();
      const result = await send(method, params, sessionId);
      signal.throwIfAborted();
      return result;
    };
    // Reports every target; only popups of the tabs are acted on, and the rest dropped above.
    await step('Target.setDiscoverTargets', { discover: true });
    const { targetId } = (await send('Target.createTarget', NEW_TAB)) as { targetId: string };
    const tab: Tab = { targetId, sessionId: '', children: new Set(), listeners: [], onPopup };
    /** Hears nothing more of the tab: its own and its children's events stop here. */
    const silence = () => {
      for (const id of [tab.sessionId, ...tab.children]) bySession.delete(id);
    };
    // Its popups are still closed until the tab itself is.
    byTarget.set(targetId, tab);
    try {
      ({ sessionId: tab.sessionId } = (await step('Target.attachToTarget', {
        targetId,
        flatten: true,
      })) as { sessionId: string });
      bySession.set(tab.sessionId, tab);
      await step('Target.setAutoAttach', AUTO_ATTACH, tab.sessionId);
    } catch (error) {
      silence();
      byTarget.delete(targetId);
      // Not awaited: a target the browser does not close goes with the connection.
      send('Target.closeTarget', { targetId }).catch(() => undefined);
      throw error;
    }
    signal.addEventListener('abort', silence, { once: true });
    const { sessionId } = tab;
    const cdp: CdpSession = {
      async sendCommand(method, params = {}, child) {
        signal.throwIfAborted();
        const url = (params as { url?: unknown }).url;
        if (!TAB_COMMANDS.has(method) || (method === 'Page.navigate' && !opensWebOrBlank(url))) {
          throw new CdpCommandError(`${method} is not sent to the user's browser.`);
        }
        if (child !== undefined && !tab.children.has(child)) {
          throw new CdpCommandError('That session is not one of this tab.');
        }
        return send(method, params, child ?? sessionId);
      },
      on(_event, listener) {
        tab.listeners.push(listener);
        return cdp;
      },
    };
    return {
      cdp,
      async close() {
        silence();
        byTarget.delete(targetId);
        await send('Target.closeTarget', { targetId });
      },
    };
  }

  return { openTab, closed, close: () => socket.close() };
}

/**
 * Opens the WebSocket and waits, with no time limit, for the user to allow it in the browser;
 * the allow wait is up for as long as that lasts. `signal` ends it.
 */
function openSocket(
  url: string,
  waitForUser: UserBrowserWaitHook,
  signal: AbortSignal,
  refuse: Refusal
): Promise<UserBrowserConnection> {
  return new Promise((resolve, reject) => {
    // A stop that came while the browser was checked never fires the listener below.
    signal.throwIfAborted();
    // `ws` sends no Origin header, which the browser would refuse at once, and sets no handshake
    // timeout unless asked to.
    const socket = new WebSocket(url);
    const endWait = waitForUser('allow');
    let settled = false;
    const settle = (finish: () => void) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener('abort', onAbort);
      endWait();
      finish();
    };
    const fail = (error: unknown) => {
      settle(() => reject(error));
      socket.terminate();
    };
    const onAbort = () => fail(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    socket.on('unexpected-response', (_request, response) =>
      // 403 is how the browser answers when the user declines.
      fail(response.statusCode === 403 ? refuse.denied() : refuse.unavailable())
    );
    socket.on('error', () => fail(refuse.unavailable()));
    socket.on('open', () => settle(() => resolve(browserOver(socket))));
  });
}

/**
 * Connects to the browser the id names. Each refusal is a `BrowserOperationError` with one of
 * the `BROWSER_USER_*` codes; an abort rejects with `signal.reason`.
 */
export async function connectUserBrowser(params: {
  browser: UserBrowserId;
  /** `~/Library/Application Support`, where every known browser keeps its user-data-dir. */
  applicationSupportDir: string;
  runner: CommandRunner;
  /** Told while the user is asked to turn remote debugging on, then to allow Pantaray in. */
  waitForUser: UserBrowserWaitHook;
  signal: AbortSignal;
}): Promise<UserBrowserConnection> {
  const browser = KNOWN_BROWSERS[params.browser];
  const refuse = refusal(browser);
  const userDataDir = path.join(params.applicationSupportDir, browser.userDataDir);
  if (!(await browserRunning(userDataDir, refuse))) throw refuse.notRunning();
  params.signal.throwIfAborted();
  const endpoint = await endpointWhenOn(
    userDataDir,
    browser,
    params.runner,
    params.waitForUser,
    params.signal,
    refuse
  );
  return openSocket(
    `ws://127.0.0.1:${endpoint.port}${endpoint.path}`,
    params.waitForUser,
    params.signal,
    refuse
  );
}
