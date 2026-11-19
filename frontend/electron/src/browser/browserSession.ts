/**
 * The browser sessions Electron main keeps for the runtime, one per `session_id`.
 *
 * A session is one hidden window (`pageWindowOptions`) in a partition of its own that lives in
 * memory only, driven through `webContents.debugger`. It is never shown or focused. What its page
 * may load is `pagePolicy`'s decision under the latest policy the runtime sent; with the network
 * off, a dead proxy also catches what never passes `webRequest`.
 *
 * Nothing on the page may open native UI on the user's display or a connection `webRequest`
 * does not see, in any frame:
 * - Every document is sandboxed without `allow-modals` (`DOCUMENT_POLICY`), so Blink itself
 *   ignores `print()`, `execCommand('print')` and the dialogs, and it may start no worker: a
 *   shared or service worker's WebSocket has no frame, and Electron passes only a frame's
 *   WebSocket through `webRequest` (`ElectronBrowserClient::WillInterceptWebSocket`).
 * - Every document disallows picture-in-picture (`DOCUMENT_PERMISSIONS`), which hides the video
 *   controls' button: it enters picture-in-picture without the check the page's API makes.
 * - Every document of every frame, an out-of-process one included, loses before its first
 *   script the APIs that connect past `webRequest` or open OS UI the sandbox and the permission
 *   handlers leave open (`pageGuardPreload`), and every frame's file chooser is intercepted.
 * Permissions, downloads and new windows are refused. Each refusal is reported with the next
 * answer, as is every host the page reached, once.
 *
 * Operations on one session run one after another, each within its budget. A session ends on
 * close, on a renderer crash, past an operation's budget, when its window is closed (the app
 * quits), or when the backend generation that opened it ends. A closed or crashed id is refused
 * from then on; after a timeout the next `navigate` opens a new session under the same id.
 */

import {
  BrowserWindow,
  session as electronSessions,
  type Debugger,
  type Session,
  type WebContents,
} from 'electron';
import path from 'node:path';

import {
  BROWSER_MAX_BLOCKED_REQUESTS,
  type BrowserOperationAnswer,
  type BrowserOperationRequest,
  type BrowserPageData,
  type BrowserPagePolicy,
} from './browserContract';
import {
  decideBrowserRequest,
  serveFile,
  type BrowserBlockedReason,
  type ContactedHost,
  type FileAccessScope,
} from './pagePolicy';
import { GUARDED_APIS, removeGuardedApis } from './pageGuardPreload';
import { PAGE_VIEWPORT, pageWindowOptions } from './pageWindowOptions';

type OkResult = Extract<BrowserOperationAnswer['result'], { status: 'ok' }>;
export type BrowserFailureCode = Extract<
  BrowserOperationAnswer['result'],
  { status: 'failed' }
>['code'];
type Note = OkResult['notes'][number];
type BlockedRequest = BrowserPageData['blocked'][number];

/**
 * Per-operation budgets. The runtime's relay waits for the longest of them plus a margin for the
 * answer's round trip (`_ELECTRON_LONGEST_OPERATION_BUDGET_SECONDS` in
 * `agents/.../local_runtime/runtime/browser_relay.py`), so a longer budget here needs that
 * constant raised with it.
 */
export const BROWSER_NAVIGATION_BUDGET_MS = 20_000;
export const BROWSER_OPERATION_BUDGET_MS = 10_000;

const MAX_POPUPS_REPORTED = 10;
// Design limit: the console keeps its latest lines, each cut at a length, and a cut answers
// `console_too_long`; raise them when that hides lines the agent needed.
const MAX_CONSOLE_LINES = 100;
const MAX_CONSOLE_LINE_CHARS = 500;
/**
 * Added to every document's own policy. `allow-same-origin` keeps cookies and storage working;
 * `allow-popups` and `allow-downloads` let those attempts reach the handlers that refuse and
 * report them.
 */
const DOCUMENT_POLICY =
  "sandbox allow-scripts allow-same-origin allow-forms allow-popups allow-downloads; worker-src 'none'";
/** Added after every document's own `Permissions-Policy`, so a page cannot loosen it. */
const DOCUMENT_PERMISSIONS = 'picture-in-picture=()';
/** `removeGuardedApis` for the `about:blank` documents the preload never reaches. */
const PAGE_GUARD_SCRIPT = `(${removeGuardedApis.toString()})(${JSON.stringify(GUARDED_APIS)});`;
const PAGE_GUARD_PRELOAD = path.join(__dirname, 'pageGuardPreload.js');
/** Blink's console message for a dialog or print a sandboxed document asked for. */
const IGNORED_MODAL = /^Ignored call to '/;
/** Every connection Chromium makes outside `webRequest` (preconnects) goes nowhere, loopback included. */
const DEAD_PROXY = { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' };

/** A failure the answer reports by its code. */
export class BrowserOperationError extends Error {
  constructor(
    readonly code: BrowserFailureCode,
    message: string
  ) {
    super(message);
  }
}

/** The page an operation works on, with its console since the session opened. */
export type BrowserPage = {
  contents: WebContents;
  cdp: Debugger;
  console: { lines: NonNullable<BrowserPageData['console']>; cut: boolean };
};

/** What happened on the page since the session's previous answer. */
export type PageReport = Pick<BrowserPageData, 'popups' | 'blocked'> & {
  notes: Note[];
  network: ContactedHost[];
};

export type OperationSettled<T> =
  | { status: 'ok'; value: T; url: string; title: string; report: PageReport }
  | { status: 'failed'; code: BrowserFailureCode; message: string; network: ContactedHost[] };

type PageSession = BrowserPage & {
  id: string;
  window: BrowserWindow;
  ses: Session;
  backendPort: number;
  policy: BrowserPagePolicy;
  /** The network setting the proxy was last set for; null before the first operation. */
  proxiedNetwork: boolean | null;
  contacted: ContactedHost[];
  unreported: ContactedHost[];
  notes: Set<Note>;
  popups: string[];
  blocked: BlockedRequest[];
  /** Attaching CDP and pinning the viewport, before the first operation. */
  ready: Promise<void>;
  queue: Promise<unknown>;
  /** Why the session ended; set once. */
  end: BrowserOperationError | null;
  /** Rejects with `end` when the session ends, so an operation in flight stops waiting. */
  ended: Promise<never>;
  stop: (reason: BrowserOperationError) => void;
};

type LoggerLike = { error?: (name: string, payload?: unknown) => void };

const sessionGone = () =>
  new BrowserOperationError('BROWSER_SESSION_GONE', 'This browser session was closed.');

function fileScope(policy: BrowserPagePolicy): FileAccessScope {
  return {
    fileRoots: policy.file_roots,
    privateRoots: policy.private_roots,
    privateRootExceptions: policy.private_root_exceptions,
    secretPaths: policy.secret_paths,
  };
}

function reach(page: PageSession, host: ContactedHost): void {
  if (page.contacted.some((known) => known.host === host.host && known.port === host.port)) return;
  page.contacted.push(host);
  page.unreported.push(host);
}

function block(page: PageSession, reason: BrowserBlockedReason, url: string): void {
  if (page.blocked.length < BROWSER_MAX_BLOCKED_REQUESTS) page.blocked.push({ reason, url });
}

function drain(page: PageSession): PageReport {
  const report = {
    notes: [...page.notes],
    popups: page.popups,
    blocked: page.blocked,
    network: page.unreported,
  };
  page.notes = new Set();
  page.popups = [];
  page.blocked = [];
  page.unreported = [];
  return report;
}

/** The CDP events that report what the page tried; `sessionId` names the frame target. */
function watchPage(page: PageSession, logger: LoggerLike | null | undefined): void {
  page.cdp.on('message', (_event, method, params, sessionId) => {
    if (method === 'Target.attachedToTarget') {
      void guardChild(page.cdp, params.sessionId, params.targetInfo.type, logger);
    } else if (method === 'Page.javascriptDialogOpening') {
      page.notes.add('dialog_dismissed');
      // `disableDialogs` may have closed it already, which is as good.
      void send(page.cdp, 'Page.handleJavaScriptDialog', { accept: false }, sessionId).catch(
        () => undefined
      );
    } else if (method === 'Page.fileChooserOpened') {
      page.notes.add('dialog_dismissed');
    } else if (method === 'Log.entryAdded') {
      const { source, text } = params.entry;
      if (source === 'security' && IGNORED_MODAL.test(text)) page.notes.add('dialog_dismissed');
    } else if (method === 'Audits.issueAdded') {
      const csp = params.issue.details.contentSecurityPolicyIssueDetails;
      if (csp?.violatedDirective === 'worker-src' && csp.blockedURL !== undefined) {
        block(page, 'not_allowed', csp.blockedURL);
      }
    }
  });
}

function send(cdp: Debugger, method: string, params: object, sessionId: string | undefined) {
  return sessionId === undefined
    ? cdp.sendCommand(method, params)
    : cdp.sendCommand(method, params, sessionId);
}

/** Guards one frame target: its `about:blank` documents, file chooser and reports, and its child frames. */
async function guardTarget(cdp: Debugger, sessionId?: string): Promise<void> {
  await Promise.all([
    send(cdp, 'Page.enable', {}, sessionId),
    send(cdp, 'Log.enable', {}, sessionId),
    send(cdp, 'Audits.enable', {}, sessionId),
    send(cdp, 'Page.addScriptToEvaluateOnNewDocument', { source: PAGE_GUARD_SCRIPT }, sessionId),
    send(cdp, 'Page.setInterceptFileChooserDialog', { enabled: true, cancel: true }, sessionId),
    // An out-of-process frame waits until it is guarded too.
    send(
      cdp,
      'Target.setAutoAttach',
      { autoAttach: true, waitForDebuggerOnStart: true, flatten: true },
      sessionId
    ),
  ]);
}

async function guardChild(
  cdp: Debugger,
  sessionId: string,
  type: string,
  logger: LoggerLike | null | undefined
): Promise<void> {
  try {
    if (type === 'iframe') await guardTarget(cdp, sessionId);
  } catch (error) {
    logger?.error?.('BROWSER_FRAME_GUARD_ERR', { error: errorName(error) });
  } finally {
    await send(cdp, 'Runtime.runIfWaitingForDebugger', {}, sessionId).catch(() => undefined);
  }
}

/**
 * The text in a string of its own. V8 may keep a cut of a long string as a view on the whole, so
 * a kept cut would keep the page's whole message alive.
 */
function copied(text: string): string {
  return Buffer.from(text, 'utf16le').toString('utf16le');
}

/** A log names an error only by its class: its message can carry page text or a private URL. */
function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

function guardSession(page: PageSession): void {
  const { ses, contents } = page;
  ses.protocol.handle('file', async (request) => {
    const response = await serveFile(request.url, fileScope(page.policy), (reason, url) =>
      block(page, reason, url)
    );
    if (response.ok) {
      response.headers.append('Content-Security-Policy', DOCUMENT_POLICY);
      response.headers.append('Permissions-Policy', DOCUMENT_PERMISSIONS);
    }
    return response;
  });
  ses.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...details.responseHeaders };
    const append = (name: string, value: string) => {
      const key = Object.keys(headers).find((known) => known.toLowerCase() === name.toLowerCase());
      headers[key ?? name] = [...(key === undefined ? [] : headers[key]), value];
    };
    append('Content-Security-Policy', DOCUMENT_POLICY);
    append('Permissions-Policy', DOCUMENT_PERMISSIONS);
    callback({ responseHeaders: headers });
  });
  const decide = (url: string, method: string) =>
    decideBrowserRequest(
      { url, method },
      { policy: page.policy, backendPort: page.backendPort, contacted: page.contacted }
    );
  ses.webRequest.onBeforeRequest((details, callback) => {
    const decision = decide(details.url, details.method);
    if (!decision.allowed) block(page, decision.reason, details.url);
    else if (decision.reached !== null) reach(page, decision.reached);
    callback({ cancel: !decision.allowed });
  });
  // A resource hint opens a connection without passing webRequest and cannot be refused; it
  // sends only the host name, so the host is reported when the policy would let a read reach it.
  ses.on('preconnect', (_event, url) => {
    const decision = decide(url, 'GET');
    if (decision.allowed && decision.reached !== null) reach(page, decision.reached);
  });
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
  ses.on('will-download', (event) => {
    event.preventDefault();
    page.notes.add('download_refused');
  });
  // Replaces the app-wide handler, which would open web links in the user's browser.
  contents.setWindowOpenHandler(({ url }) => {
    page.notes.add('popup_refused');
    if (page.popups.length < MAX_POPUPS_REPORTED) page.popups.push(url);
    return { action: 'deny' };
  });
  // Design invariant 5; the guard script removes WebRTC itself.
  contents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
}

async function attachDebugger(contents: WebContents, cdp: Debugger): Promise<void> {
  // A command sent before the first navigation may not settle until one happens.
  await contents.loadURL('about:blank');
  cdp.attach('1.3');
  await guardTarget(cdp);
  // Device scale 1, so an image has the CSS pixel size on any display.
  await cdp.sendCommand('Emulation.setDeviceMetricsOverride', {
    width: PAGE_VIEWPORT.width,
    height: PAGE_VIEWPORT.height,
    deviceScaleFactor: 1,
    mobile: false,
  });
}

async function applyPolicy(page: PageSession, policy: BrowserPagePolicy): Promise<void> {
  page.policy = policy;
  if (page.proxiedNetwork === policy.allow_network) return;
  page.proxiedNetwork = policy.allow_network;
  await page.ses.setProxy(policy.allow_network ? { mode: 'system' } : DEAD_PROXY);
  // A connection opened while the network was on would outlive the switch.
  if (!policy.allow_network) await page.ses.closeAllConnections();
}

export type BrowserSessions = ReturnType<typeof createBrowserSessions>;

export function createBrowserSessions(params: {
  /** The backend's own port, which no page may reach under any host name. */
  getBackendPort: () => number;
  logger?: LoggerLike | null;
}) {
  const sessions = new Map<string, PageSession>();
  let partitions = 0;
  /** Ids closed or crashed; the runtime mints a new id for the run's next session. */
  const ended = new Set<string>();

  /** Ends the session; unless `reopenable`, its id is refused from then on. */
  function discard(page: PageSession, reason: BrowserOperationError, reopenable = false): void {
    if (page.end !== null) return;
    page.end = reason;
    if (!reopenable) ended.add(page.id);
    sessions.delete(page.id);
    page.stop(reason);
    if (!page.window.isDestroyed()) page.window.destroy();
    page.ses.protocol.unhandle('file');
    page.ses.webRequest.onBeforeRequest(null);
    page.ses.webRequest.onHeadersReceived(null);
    // Electron cannot destroy a session; its memory goes with its storage.
    Promise.all([page.ses.clearStorageData(), page.ses.clearCache()]).catch((error: unknown) =>
      params.logger?.error?.('BROWSER_SESSION_CLEAR_ERR', { error: errorName(error) })
    );
  }

  function open(id: string, policy: BrowserPagePolicy): PageSession {
    // No `persist:` prefix: the partition lives in memory only, and no other session, nor a
    // later one under the same id, shares it.
    partitions += 1;
    const partition = `browser-session-${partitions}`;
    const ses = electronSessions.fromPartition(partition);
    ses.registerPreloadScript({ type: 'frame', filePath: PAGE_GUARD_PRELOAD });
    const window = new BrowserWindow(pageWindowOptions(partition));
    const contents = window.webContents;
    let stop: PageSession['stop'] = () => undefined;
    const endedPromise = new Promise<never>((_resolve, reject) => (stop = reject));
    endedPromise.catch(() => undefined);
    const page: PageSession = {
      id,
      window,
      contents,
      cdp: contents.debugger,
      ses,
      backendPort: params.getBackendPort(),
      policy,
      proxiedNetwork: null,
      contacted: [],
      unreported: [],
      notes: new Set(),
      popups: [],
      blocked: [],
      console: { lines: [], cut: false },
      ready: Promise.resolve(),
      queue: Promise.resolve(),
      end: null,
      ended: endedPromise,
      stop,
    };
    guardSession(page);
    watchPage(page, params.logger);
    contents.on('render-process-gone', (_event, details) =>
      discard(
        page,
        new BrowserOperationError(
          'BROWSER_RENDERER_CRASHED',
          `The page renderer ${details.reason}.`
        )
      )
    );
    window.on('closed', () => discard(page, sessionGone()));
    contents.on('console-message', ({ level, message }) => {
      const kept = page.console;
      kept.lines.push({ level, text: copied(message.slice(0, MAX_CONSOLE_LINE_CHARS)) });
      if (message.length > MAX_CONSOLE_LINE_CHARS) kept.cut = true;
      if (kept.lines.length > MAX_CONSOLE_LINES) {
        kept.lines.shift();
        kept.cut = true;
      }
    });
    page.ready = attachDebugger(contents, page.cdp);
    page.ready.catch(() => undefined);
    sessions.set(id, page);
    return page;
  }

  async function execute<T>(
    page: PageSession,
    request: BrowserOperationRequest,
    budgetMs: number,
    work: (page: BrowserPage) => Promise<T>
  ): Promise<OperationSettled<T>> {
    const failed = (error: BrowserOperationError): OperationSettled<T> => ({
      status: 'failed',
      code: error.code,
      message: error.message,
      network: drain(page).network,
    });
    // A crash ends the operation in flight; the ones after it find the session gone.
    if (page.end !== null) return failed(sessionGone());
    let timer: NodeJS.Timeout | undefined;
    const budget = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(
        () =>
          reject(
            new BrowserOperationError(
              'BROWSER_TIMED_OUT',
              `The page did not finish within ${budgetMs / 1000} s, so its session was closed.`
            )
          ),
        budgetMs
      );
    });
    const job = (async () => {
      await page.ready;
      await applyPolicy(page, request.policy);
      return work(page);
    })();
    // The losing side settles after the answer; nothing waits for it.
    job.catch(() => undefined);
    try {
      const value = await Promise.race([job, budget, page.ended]);
      const report = drain(page);
      return {
        status: 'ok',
        value,
        url: page.contents.getURL(),
        title: page.contents.getTitle(),
        report,
      };
    } catch (error) {
      if (page.end !== null) return failed(page.end);
      if (error instanceof BrowserOperationError) {
        // The work past its budget may still act on the page, so no later operation may share it.
        if (error.code === 'BROWSER_TIMED_OUT') discard(page, error, true);
        return failed(error);
      }
      params.logger?.error?.('BROWSER_OPERATION_ERR', {
        action: request.operation.action,
        error: errorName(error),
      });
      return failed(
        new BrowserOperationError(
          'BROWSER_ACTION_FAILED',
          `The browser could not ${request.operation.action}.`
        )
      );
    } finally {
      clearTimeout(timer);
    }
  }

  function close(sessionId: string): void {
    ended.add(sessionId);
    const page = sessions.get(sessionId);
    if (page !== undefined) discard(page, sessionGone());
  }

  return {
    /**
     * Runs `work` on the session the request names, after every operation sent to it before;
     * the first `navigate` naming a new id opens the session.
     */
    run<T>(
      request: BrowserOperationRequest,
      budgetMs: number,
      work: (page: BrowserPage) => Promise<T>
    ): Promise<OperationSettled<T>> {
      const id = request.session_id;
      const refuse = (error: BrowserOperationError): Promise<OperationSettled<T>> =>
        Promise.resolve({
          status: 'failed',
          code: error.code,
          message: error.message,
          network: [],
        });
      if (ended.has(id)) return refuse(sessionGone());
      let page = sessions.get(id);
      if (page === undefined) {
        if (request.operation.action !== 'navigate') {
          return refuse(
            new BrowserOperationError('BROWSER_NO_PAGE', 'No page is open in this session.')
          );
        }
        page = open(id, request.policy);
      }
      const opened = page;
      const settled = opened.queue.then(() => execute(opened, request, budgetMs, work));
      opened.queue = settled.catch(() => undefined);
      return settled;
    },
    /** Idempotent; an id closed before its first operation arrives is refused too. */
    close,
    /** When the backend generation that opened them ends. */
    closeAll(): void {
      for (const id of [...sessions.keys()]) close(id);
    },
  };
}
