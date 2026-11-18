/**
 * The browser sessions Electron main keeps for the runtime, one per `session_id`.
 *
 * A session is one hidden window (`pageWindowOptions`) in a partition of its own that lives in
 * memory only, driven through `webContents.debugger`. It is never shown or focused. What its page
 * may load is `pagePolicy`'s decision under the latest policy the runtime sent; with the network
 * off, a dead proxy also catches what never passes `webRequest`. Permissions, downloads and new
 * windows are refused, and dialogs are dismissed; each is reported with the next answer, as is
 * every host the page reached, once.
 *
 * Operations on one session run one after another, each within its budget. A session ends on
 * close, on a renderer crash, when its window is closed (the app quits), or when the backend
 * generation that opened it ends; an id that ended is refused from then on.
 */

import {
  BrowserWindow,
  session as electronSessions,
  type Debugger,
  type Session,
  type WebContents,
} from 'electron';

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

/** The page an operation works on. */
export type BrowserPage = { contents: WebContents; cdp: Debugger };

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

/** Dismisses a page dialog; `disableDialogs` may have closed it already, which is as good. */
function dismissDialogs(page: PageSession): void {
  page.cdp.on('message', (_event, method) => {
    if (method !== 'Page.javascriptDialogOpening') return;
    page.notes.add('dialog_dismissed');
    page.cdp.sendCommand('Page.handleJavaScriptDialog', { accept: false }).catch(() => undefined);
  });
  // Leaving a page that asks to stay ignores its `beforeunload` handler.
  page.contents.on('will-prevent-unload', (event) => {
    event.preventDefault();
    page.notes.add('dialog_dismissed');
  });
}

function guardSession(page: PageSession): void {
  const { ses, contents } = page;
  ses.protocol.handle('file', (request) =>
    serveFile(request.url, fileScope(page.policy), (reason, url) => block(page, reason, url))
  );
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
  // No data path may bypass webRequest, whatever the network setting.
  contents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
}

async function attachDebugger(cdp: Debugger): Promise<void> {
  cdp.attach('1.3');
  await cdp.sendCommand('Page.enable');
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
  /** Ids that ended; the runtime mints a new id for the run's next session. */
  const ended = new Set<string>();

  function discard(page: PageSession, reason: BrowserOperationError): void {
    if (page.end !== null) return;
    page.end = reason;
    ended.add(page.id);
    sessions.delete(page.id);
    page.stop(reason);
    if (!page.window.isDestroyed()) page.window.destroy();
    page.ses.protocol.unhandle('file');
    page.ses.webRequest.onBeforeRequest(null);
    // Electron cannot destroy a session; its memory goes with its storage.
    Promise.all([page.ses.clearStorageData(), page.ses.clearCache()]).catch((error: unknown) =>
      params.logger?.error?.('BROWSER_SESSION_CLEAR_ERR', { err: error })
    );
  }

  function open(id: string, policy: BrowserPagePolicy): PageSession {
    // No `persist:` prefix: the partition lives in memory only, and no other session shares it.
    const partition = `browser-session-${id}`;
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
      ses: electronSessions.fromPartition(partition),
      backendPort: params.getBackendPort(),
      policy,
      proxiedNetwork: null,
      contacted: [],
      unreported: [],
      notes: new Set(),
      popups: [],
      blocked: [],
      ready: Promise.resolve(),
      queue: Promise.resolve(),
      end: null,
      ended: endedPromise,
      stop,
    };
    guardSession(page);
    dismissDialogs(page);
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
    page.ready = attachDebugger(page.cdp);
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
              `The page did not finish within ${budgetMs / 1000} s.`
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
        // The page keeps what loaded; a load still running would change it under the next call.
        if (error.code === 'BROWSER_TIMED_OUT') page.contents.stop();
        return failed(error);
      }
      params.logger?.error?.('BROWSER_OPERATION_ERR', {
        action: request.operation.action,
        err: error,
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
