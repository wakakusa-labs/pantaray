/**
 * The tab of a session's own in the user's browser, guarded before it runs, and the page an
 * operation drives in it. The tab and each frame and worker it attaches are guarded:
 * - every document loses, before its first script, the app page's guarded APIs plus WebSocket,
 *   SharedWorker and service workers, which Fetch interception does not see; a worker loses
 *   WebSocket and WebTransport, and service workers are bypassed;
 * - every request pauses in Fetch and is judged by `decideUserBrowserRequest`, each redirect hop
 *   on its own; a document answered as an attachment, or with a type the browser does not
 *   display (`opensAsDownload`), is refused, so nothing is downloaded;
 * - JavaScript dialogs are dismissed and file choosers refused.
 * A frame or worker whose guard failed stays paused and never runs. Each refusal and every host
 * the tab reached goes to the session's `TabContext`, which reports it with the next answer.
 *
 * The tab lives under one signal (`UserBrowserConnection.openTab`): once it aborts, nothing more
 * is sent for the tab, guards and operations alike, and none of its events arrive.
 */

import { BROWSER_MAX_BLOCKED_REQUESTS, type BrowserPageData } from './browserContract';
import { BrowserOperationError, type PageReport } from './browserSession';
import type { CdpPage, CdpSession } from './cdpSession';
import { GUARDED_APIS, removeGuardedApis } from './pageGuardPreload';
import { PAGE_VIEWPORT } from './pageWindowOptions';
import {
  decideUserBrowserRequest,
  type ContactedHost,
  type BrowserBlockedReason,
} from './pagePolicy';
import type { UserBrowserConnection, UserBrowserTab } from './userBrowserConnection';

/** The page an operation drives in the user's browser. */
export type UserBrowserPage = CdpPage & {
  /** Opens a web URL in the tab; a main-frame load the tab refused resolves, reported as blocked. */
  navigate(url: string): Promise<void>;
  back(): Promise<void>;
};

type Note = PageReport['notes'][number];

/** The session's rules the tab judges by, under the latest request, and what it reports to. */
export type TabContext = {
  readHosts: string[];
  writeHosts: string[];
  backendPort: number;
  contacted: ContactedHost[];
  unreported: ContactedHost[];
  notes: Set<Note>;
  popups: string[];
  blocked: BrowserPageData['blocked'];
};

export type GuardedTab = {
  handle: UserBrowserTab;
  page: UserBrowserPage;
  location(): Promise<{ url: string; title: string }>;
};

type ConsoleLevel = NonNullable<BrowserPageData['console']>[number]['level'];
type LoggerLike = { error?: (name: string, payload?: unknown) => void };

const MAX_POPUPS_REPORTED = 10;
// The app page's console limits (`browserSession`).
const MAX_CONSOLE_LINES = 100;
const MAX_CONSOLE_LINE_CHARS = 500;
const FRAME_GUARDED_APIS = [
  ...GUARDED_APIS,
  ['globalThis', 'WebSocket'],
  ['globalThis', 'SharedWorker'],
  ['Navigator.prototype', 'serviceWorker'],
] as const;
const FRAME_GUARD_SCRIPT = `(${removeGuardedApis.toString()})(${JSON.stringify(FRAME_GUARDED_APIS)});`;
/** A dedicated worker has none of the frame's pickers; it can still open these connections. */
const WORKER_GUARD_SCRIPT =
  'delete globalThis.WebSocket; delete globalThis.WebTransport; ' +
  'delete WorkerNavigator.prototype.serviceWorker;';
/** Every request before it is sent, and every document's response, for its disposition. */
const FETCH_PATTERNS = [
  { urlPattern: '*', requestStage: 'Request' },
  { urlPattern: '*', resourceType: 'Document', requestStage: 'Response' },
];
const ATTACHMENT = /^\s*attachment/i;
/** The document types the browser displays in the tab; it downloads any other. */
const DISPLAYED_TYPE = /^(text\/[^;]+|image\/[^;]+|application\/(pdf|json|xml|[^;]+\+xml))$/;

type RequestPaused = {
  requestId: string;
  request: { url: string; method: string };
  frameId: string;
  resourceType: string;
  responseStatusCode?: number;
  responseErrorReason?: string;
  responseHeaders?: { name: string; value: string }[];
};
type ConsoleCalled = { type: string; args: { value?: unknown; description?: string }[] };
type NavigationHistory = {
  currentIndex: number;
  entries: { id: number; url: string; title: string }[];
};

/** A log names an error only by its class: its message can carry page text or a private URL. */
function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

/** The text in a string of its own, so a kept cut does not keep the page's whole message alive. */
function copied(text: string): string {
  return Buffer.from(text, 'utf16le').toString('utf16le');
}

function consoleLevel(type: string): ConsoleLevel {
  if (type === 'warning' || type === 'debug' || type === 'info') return type;
  return type === 'error' || type === 'assert' ? 'error' : 'log';
}

function reach(context: TabContext, host: ContactedHost): void {
  if (context.contacted.some((known) => known.host === host.host && known.port === host.port)) {
    return;
  }
  context.contacted.push(host);
  context.unreported.push(host);
}

function block(context: TabContext, reason: BrowserBlockedReason, url: string): void {
  if (context.blocked.length < BROWSER_MAX_BLOCKED_REQUESTS) context.blocked.push({ reason, url });
}

/**
 * Whether a document response would be saved rather than shown: an attachment, or a body of a
 * type the browser does not display. A failed load, a redirect and an empty answer have no body
 * to save; a body without a type is refused too, since the browser would guess it.
 */
function opensAsDownload(paused: RequestPaused): boolean {
  const status = paused.responseStatusCode;
  if (status === undefined || (status >= 300 && status < 400) || status === 204 || status === 205) {
    return false;
  }
  const header = (name: string) =>
    paused.responseHeaders?.find((entry) => entry.name.toLowerCase() === name)?.value;
  if (ATTACHMENT.test(header('content-disposition') ?? '')) return true;
  const type = (header('content-type') ?? '').split(';')[0].trim().toLowerCase();
  return !DISPLAYED_TYPE.test(type);
}

/** Guards a frame target, the tab's own (`sessionId` undefined) or an out-of-process frame. */
async function guardFrame(cdp: CdpSession, sessionId?: string): Promise<void> {
  await Promise.all([
    cdp.sendCommand('Page.enable', {}, sessionId),
    cdp.sendCommand(
      'Page.addScriptToEvaluateOnNewDocument',
      { source: FRAME_GUARD_SCRIPT, runImmediately: true },
      sessionId
    ),
    cdp.sendCommand(
      'Page.setInterceptFileChooserDialog',
      { enabled: true, cancel: true },
      sessionId
    ),
    ...bypassServiceWorkers(cdp, sessionId),
    cdp.sendCommand('Fetch.enable', { patterns: FETCH_PATTERNS }, sessionId),
  ]);
}

/**
 * Keeps the site's installed service worker out of the target's requests: one would make its
 * own requests, which no Fetch here sees. Chrome ignores the bypass until Network is enabled;
 * no response bodies are kept, since nothing here reads them.
 */
function bypassServiceWorkers(cdp: CdpSession, sessionId: string | undefined): Promise<unknown>[] {
  return [
    cdp.sendCommand(
      'Network.enable',
      { maxTotalBufferSize: 0, maxResourceBufferSize: 0 },
      sessionId
    ),
    cdp.sendCommand('Network.setBypassServiceWorker', { bypass: true }, sessionId),
  ];
}

/**
 * Chrome has no Fetch domain on a dedicated worker: the worker's script, imports and fetches
 * pause in its parent frame's session, which the frame's own guard already judges. Only a worker
 * started by another worker loads its script unseen: a same-origin read, which would be allowed.
 */
async function guardWorker(cdp: CdpSession, sessionId: string): Promise<void> {
  await Promise.all([
    ...bypassServiceWorkers(cdp, sessionId),
    cdp.sendCommand('Runtime.evaluate', { expression: WORKER_GUARD_SCRIPT }, sessionId),
  ]);
}

/**
 * Opens and guards a tab and watches what it does, under `signal`. Any failure closes the tab
 * and rejects: an unguarded tab is never used.
 */
export async function openGuardedTab(
  context: TabContext,
  connection: UserBrowserConnection,
  signal: AbortSignal,
  logger: LoggerLike | null | undefined
): Promise<GuardedTab> {
  let handle: UserBrowserTab | undefined;
  try {
    handle = await connection.openTab((url) => {
      context.notes.add('popup_refused');
      if (context.popups.length < MAX_POPUPS_REPORTED) context.popups.push(url);
    }, signal);
    const tab = watchTab(context, handle, logger);
    await tab.ready;
    return tab;
  } catch (error) {
    if (!signal.aborted) logger?.error?.('USER_BROWSER_TAB_GUARD_ERR', { error: errorName(error) });
    // The tab goes with the connection if it cannot be closed now.
    handle?.close().catch(() => undefined);
    throw error;
  }
}

function watchTab(
  context: TabContext,
  handle: UserBrowserTab,
  logger: LoggerLike | null | undefined
): GuardedTab & { ready: Promise<void> } {
  const { cdp } = handle;
  const console: CdpPage['console'] = { lines: [], cut: false };
  let mainFrameId: string | null = null;
  /** Main-frame documents the tab refused, so a navigate they failed still answers. */
  let refusedMainFrame = 0;
  /** The main frame's navigation: commits so far, the latest document, and whether it loaded. */
  const nav = { commits: 0, loader: null as string | null, loaded: false };
  const waiters = new Set<() => void>();
  const until = (check: () => boolean) =>
    new Promise<void>((resolve) => {
      const waiter = () => {
        if (!check()) return;
        waiters.delete(waiter);
        resolve();
      };
      waiters.add(waiter);
      waiter();
    });

  function onPaused(paused: RequestPaused, sessionId: string | undefined): void {
    const mainFrame = paused.resourceType === 'Document' && paused.frameId === mainFrameId;
    const answer = (method: string, params: object = {}) =>
      // A request whose frame went away needs no answer.
      cdp
        .sendCommand(method, { requestId: paused.requestId, ...params }, sessionId)
        .catch(() => undefined);
    const refuse = () => {
      if (mainFrame) refusedMainFrame += 1;
      return answer('Fetch.failRequest', { errorReason: 'BlockedByClient' });
    };
    if (paused.responseStatusCode !== undefined || paused.responseErrorReason !== undefined) {
      if (opensAsDownload(paused)) {
        context.notes.add('download_refused');
        return void refuse();
      }
      return void answer('Fetch.continueRequest');
    }
    const decision = decideUserBrowserRequest(
      { url: paused.request.url, method: paused.request.method, mainFrameDocument: mainFrame },
      {
        readHosts: context.readHosts,
        writeHosts: context.writeHosts,
        backendPort: context.backendPort,
        contacted: context.contacted,
      }
    );
    if (!decision.allowed) {
      // The user's extensions load their own files into pages; refused, but no news to the model.
      if (!paused.request.url.startsWith('chrome-extension:')) {
        block(context, decision.reason, paused.request.url);
      }
      return void refuse();
    }
    if (decision.reached !== null) reach(context, decision.reached);
    void answer('Fetch.continueRequest');
  }

  async function guardChild(sessionId: string, type: string): Promise<void> {
    try {
      if (type === 'iframe') await guardFrame(cdp, sessionId);
      else await guardWorker(cdp, sessionId);
      await cdp.sendCommand('Runtime.runIfWaitingForDebugger', {}, sessionId);
    } catch (error) {
      // Left waiting for the debugger: it never runs unguarded.
      logger?.error?.('USER_BROWSER_CHILD_GUARD_ERR', { error: errorName(error) });
    }
  }

  function onConsole(called: ConsoleCalled): void {
    const text = called.args
      .map((arg) => (arg.value === undefined ? (arg.description ?? '') : String(arg.value)))
      .join(' ');
    console.lines.push({
      level: consoleLevel(called.type),
      text: copied(text.slice(0, MAX_CONSOLE_LINE_CHARS)),
    });
    if (text.length > MAX_CONSOLE_LINE_CHARS) console.cut = true;
    if (console.lines.length > MAX_CONSOLE_LINES) {
      console.lines.shift();
      console.cut = true;
    }
  }

  cdp.on('message', (_event, method, params, sessionId) => {
    if (method === 'Fetch.requestPaused') {
      onPaused(params as RequestPaused, sessionId);
    } else if (method === 'Target.attachedToTarget') {
      const attached = params as { sessionId: string; targetInfo: { type: string } };
      void guardChild(attached.sessionId, attached.targetInfo.type);
    } else if (method === 'Page.javascriptDialogOpening') {
      context.notes.add('dialog_dismissed');
      void cdp
        .sendCommand('Page.handleJavaScriptDialog', { accept: false }, sessionId)
        .catch(() => undefined);
    } else if (method === 'Page.fileChooserOpened') {
      context.notes.add('dialog_dismissed');
    } else if (sessionId !== undefined) {
      return;
    } else if (method === 'Runtime.consoleAPICalled') {
      onConsole(params as ConsoleCalled);
    } else if (method === 'Page.frameNavigated') {
      const { frame, type } = params as {
        frame: { parentId?: string; loaderId: string };
        type?: string;
      };
      if (frame.parentId !== undefined) return;
      nav.commits += 1;
      nav.loader = frame.loaderId;
      // A page restored from the back-forward cache fires no load.
      nav.loaded = type === 'BackForwardCacheRestore';
    } else if (method === 'Page.navigatedWithinDocument') {
      if ((params as { frameId: string }).frameId !== mainFrameId) return;
      nav.commits += 1;
      nav.loaded = true;
    } else if (method === 'Page.loadEventFired') {
      nav.loaded = true;
    }
    for (const waiter of [...waiters]) waiter();
  });

  const ready = (async () => {
    const { frameTree } = (await cdp.sendCommand('Page.getFrameTree')) as {
      frameTree: { frame: { id: string } };
    };
    mainFrameId = frameTree.frame.id;
    await guardFrame(cdp);
    await Promise.all([
      cdp.sendCommand('Runtime.enable'),
      // Device scale 1, so an image has the CSS pixel size on any display.
      cdp.sendCommand('Emulation.setDeviceMetricsOverride', {
        width: PAGE_VIEWPORT.width,
        height: PAGE_VIEWPORT.height,
        deviceScaleFactor: 1,
        mobile: false,
      }),
    ]);
  })();

  const history = async () =>
    (await cdp.sendCommand('Page.getNavigationHistory')) as NavigationHistory;

  const page: UserBrowserPage = {
    cdp,
    console,
    async navigate(url) {
      const refusedBefore = refusedMainFrame;
      const { loaderId, errorText } = (await cdp.sendCommand('Page.navigate', { url })) as {
        loaderId?: string;
        errorText?: string;
      };
      if (errorText !== undefined) {
        // Refused by the tab's rules: reported in `blocked` or the notes, not a failed load.
        if (refusedMainFrame > refusedBefore) return;
        throw new BrowserOperationError(
          'BROWSER_NAVIGATION_FAILED',
          `The page did not load (${errorText}).`
        );
      }
      // No loader: a navigation within the document, already done.
      if (loaderId !== undefined) await until(() => nav.loader === loaderId && nav.loaded);
    },
    async back() {
      const { currentIndex, entries } = await history();
      if (currentIndex < 1) {
        throw new BrowserOperationError(
          'BROWSER_ACTION_FAILED',
          'There is no earlier page to go back to.'
        );
      }
      const commitsBefore = nav.commits;
      await cdp.sendCommand('Page.navigateToHistoryEntry', {
        entryId: entries[currentIndex - 1].id,
      });
      await until(() => nav.commits > commitsBefore && nav.loaded);
    },
  };
  return {
    handle,
    page,
    ready,
    async location() {
      const { currentIndex, entries } = await history();
      return { url: entries[currentIndex].url, title: entries[currentIndex].title };
    },
  };
}
