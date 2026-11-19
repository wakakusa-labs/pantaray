/**
 * Connecting to the user's own browser (Chrome 144+ auto-connect): Electron main finds the
 * running browser through its user-data-dir, checks that what listens on its debugging port is
 * that browser by code signature, and opens one WebSocket to it, which the browser holds until
 * the user allows it. A refusal names the browser and tells the user only what they can act on.
 *
 * The connection reaches the whole browser, but carries only what this run's own tabs need: at
 * the browser level it sends the few `Target` commands below, the target-naming ones only for a
 * target it created or one such a target opened; within a session, only to a session it attached
 * to one of those targets or one such a session auto-attached. It passes on no event about any
 * other target or from any other session, so the user's tabs never reach this process.
 */

import type { ClientRequest } from 'node:http';
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import type { UserBrowserId } from './browserContract';
import { BrowserOperationError } from './browserSession';
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

export type UserBrowserConnection = {
  /** Commands without a `sessionId` go to the browser itself, within the allowlist below. */
  cdp: CdpSession;
  /** Settles once the socket has closed, whichever side closed it. */
  closed: Promise<void>;
  close(): void;
};

/** A command the browser refused, or one this connection does not send. */
export class CdpCommandError extends Error {}

const LOCK_FILE = 'SingletonLock';
const PORT_FILE = 'DevToolsActivePort';
/** Chromium's lock is a symlink to `<hostname>-<pid>`. */
const LOCK_TARGET = /-(\d+)$/;
const BROWSER_PATH = /^\/devtools\/browser\/[0-9A-Za-z-]+$/;
/** What macOS answers when privacy settings keep Pantaray out of the browser's files. */
const BLOCKED_ERRNO = new Set(['EPERM', 'EACCES']);

/** Browser-level commands this connection sends; each but the first names a target of its own. */
const BROWSER_COMMANDS = new Set([
  'Target.createTarget',
  'Target.attachToTarget',
  'Target.getTargetInfo',
  'Target.closeTarget',
  // Reports every target; only the ones of this connection's own are passed on.
  'Target.setDiscoverTargets',
]);
const TARGET_NAMING = new Set([
  'Target.attachToTarget',
  'Target.getTargetInfo',
  'Target.closeTarget',
]);

const refusal = (browser: KnownBrowser) => ({
  notRunning: () =>
    new BrowserOperationError('BROWSER_USER_NOT_RUNNING', `${browser.name} is not running.`),
  debuggingOff: () =>
    new BrowserOperationError(
      'BROWSER_USER_DEBUGGING_OFF',
      `${browser.name} does not allow remote debugging; turn it on at ${browser.inspectUrl}.`
    ),
  fileBlocked: () =>
    new BrowserOperationError(
      'BROWSER_USER_FILE_BLOCKED',
      `macOS did not let Pantaray read ${browser.name}'s files; allow it in System Settings › ` +
        'Privacy & Security › Files & Folders.'
    ),
  denied: () =>
    new BrowserOperationError(
      'BROWSER_USER_DENIED',
      `The user did not allow Pantaray to use ${browser.name}.`
    ),
  unavailable: () =>
    new BrowserOperationError('BROWSER_USER_UNAVAILABLE', `${browser.name} could not be used.`),
});
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

/** The browser's WebSocket endpoint as it wrote it: the port on line 1, the path on line 2. */
async function endpointOf(
  userDataDir: string,
  refuse: Refusal
): Promise<{ port: number; path: string }> {
  let text: string;
  try {
    text = await fs.promises.readFile(path.join(userDataDir, PORT_FILE), 'utf8');
  } catch (error) {
    if (errnoOf(error) === 'ENOENT') throw refuse.debuggingOff();
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
 * team. A file left by a browser that quit can name a port another process took since.
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
): Promise<void> {
  const listing = await run(
    runner,
    '/usr/sbin/lsof',
    ['-nP', `-iTCP@127.0.0.1:${port}`, '-sTCP:LISTEN', '-Fp'],
    signal,
    refuse
  );
  // lsof exits 1 when nothing matches.
  if (listing.exitCode !== 0 && listing.exitCode !== 1) throw refuse.unavailable();
  const pids = new Set(
    listing.stdout
      .split('\n')
      .filter((line) => /^p\d+$/.test(line))
      .map((line) => line.slice(1))
  );
  // Nothing listens once the user turned remote debugging off, though the file stays.
  if (pids.size === 0) throw refuse.debuggingOff();
  if (pids.size > 1) throw refuse.unavailable();
  const requirement =
    `=anchor apple generic and certificate leaf[subject.OU] = "${browser.teamId}" and ` +
    `(identifier "${browser.bundleId}" or identifier "${browser.bundleId}.helper")`;
  const verified = await run(
    runner,
    '/usr/bin/codesign',
    ['--verify', '-R', requirement, [...pids][0]],
    signal,
    refuse
  );
  if (verified.exitCode !== 0) throw refuse.unavailable();
}

type CdpMessage = {
  id?: number;
  result?: unknown;
  error?: { message?: string };
  method?: string;
  params?: {
    sessionId?: string;
    targetId?: string;
    targetInfo?: { targetId?: string; openerId?: string };
  };
  sessionId?: string;
};
type Listener = Parameters<CdpSession['on']>[1];

/** The CDP client on an open socket. */
function cdpOver(socket: WebSocketClient): UserBrowserConnection {
  let nextId = 0;
  const pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (e: Error) => void }
  >();
  const listeners: Listener[] = [];
  /** Targets this connection created, the ones they opened, and the ones their sessions attached. */
  const own = new Set<string>();
  /** Sessions attached to those targets. */
  const ownSessions = new Set<string>();
  let isClosed = false;
  let onClosed: () => void = () => undefined;
  const closed = new Promise<void>((resolve) => (onClosed = resolve));

  /**
   * An event passes when it comes from a session of this connection's own or, at the browser
   * level, is about a target of its own; the sessions those events attach and detach are kept.
   */
  function admitted(message: CdpMessage): boolean {
    const info = message.params?.targetInfo;
    let passes: boolean;
    if (message.sessionId !== undefined) {
      passes = ownSessions.has(message.sessionId);
      // What a session of ours auto-attaches is a frame or worker of our tab.
      if (passes && message.method === 'Target.attachedToTarget' && info?.targetId !== undefined) {
        own.add(info.targetId);
      }
    } else {
      if (info?.openerId !== undefined && own.has(info.openerId) && info.targetId !== undefined) {
        own.add(info.targetId);
      }
      const targetId = info?.targetId ?? message.params?.targetId;
      passes = targetId !== undefined && own.has(targetId);
    }
    const childSession = message.params?.sessionId;
    if (passes && childSession !== undefined) {
      if (message.method === 'Target.attachedToTarget') ownSessions.add(childSession);
      if (message.method === 'Target.detachedFromTarget') ownSessions.delete(childSession);
    }
    return passes;
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
    } else if (message.method !== undefined && admitted(message)) {
      for (const listener of listeners) {
        listener({}, message.method, message.params ?? {}, message.sessionId);
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

  const cdp: CdpSession = {
    async sendCommand(method, params = {}, sessionId) {
      const targetId = (params as { targetId?: unknown }).targetId;
      const ownTarget = typeof targetId === 'string' && own.has(targetId);
      // An empty session id names the browser itself; like any other unknown id, it is not ours.
      const allowed =
        sessionId === undefined
          ? BROWSER_COMMANDS.has(method) && (!TARGET_NAMING.has(method) || ownTarget)
          : ownSessions.has(sessionId);
      if (!allowed) throw new CdpCommandError(`${method} is not sent to the user's browser.`);
      // `ws` drops what is sent after the close, which would never be answered.
      if (isClosed) throw new CdpCommandError('The connection to the browser closed.');
      nextId += 1;
      const id = nextId;
      const result = new Promise<unknown>((resolve, reject) =>
        pending.set(id, { resolve, reject })
      );
      socket.send(JSON.stringify({ id, method, params, sessionId }));
      const value = await result;
      // An event about the new target that came before this answer was not passed on.
      if (method === 'Target.createTarget') own.add((value as { targetId: string }).targetId);
      if (method === 'Target.attachToTarget' && sessionId === undefined) {
        ownSessions.add((value as { sessionId: string }).sessionId);
      }
      return value;
    },
    on(_event, listener) {
      listeners.push(listener);
      return cdp;
    },
  };
  return { cdp, closed, close: () => socket.close() };
}

/**
 * Opens the WebSocket and waits, with no time limit, for the user to allow it in the browser;
 * the notice is up for as long as that wait lasts. `signal` ends the wait.
 */
function openSocket(
  url: string,
  browser: KnownBrowser,
  showNotice: (browser: KnownBrowser) => () => void,
  signal: AbortSignal,
  refuse: Refusal
): Promise<UserBrowserConnection> {
  return new Promise((resolve, reject) => {
    // `ws` sends no Origin header, which the browser would refuse at once, and sets no handshake
    // timeout unless asked to.
    const socket = new WebSocket(url);
    const closeNotice = showNotice(browser);
    let settled = false;
    const settle = (finish: () => void) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener('abort', onAbort);
      closeNotice();
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
    socket.on('open', () => settle(() => resolve(cdpOver(socket))));
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
  showNotice: (browser: KnownBrowser) => () => void;
  signal: AbortSignal;
}): Promise<UserBrowserConnection> {
  const browser = KNOWN_BROWSERS[params.browser];
  const refuse = refusal(browser);
  const userDataDir = path.join(params.applicationSupportDir, browser.userDataDir);
  if (!(await browserRunning(userDataDir, refuse))) throw refuse.notRunning();
  params.signal.throwIfAborted();
  const endpoint = await endpointOf(userDataDir, refuse);
  params.signal.throwIfAborted();
  await verifyListener(endpoint.port, browser, params.runner, params.signal, refuse);
  params.signal.throwIfAborted();
  return openSocket(
    `ws://127.0.0.1:${endpoint.port}${endpoint.path}`,
    browser,
    params.showNotice,
    params.signal,
    refuse
  );
}
