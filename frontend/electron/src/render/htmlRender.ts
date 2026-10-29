/**
 * Printing one HTML file to PDF, for the Action runtime's HTML tools and for the task
 * preview (`htmlPreview`).
 *
 * The runtime cannot start a browser inside the command sandbox; it asks here, the
 * Chromium shipped with the app prints the page in a hidden window, and the PDF is
 * handed back through `{artifact_root}/.tmp/html-renders/{render_request_id}.pdf`
 * plus an answer posted to the local backend. The runtime treats the answer's size
 * and digest as claims and re-reads the file itself.
 *
 * The page is the agent's own output and runs with JavaScript (charts drawn by
 * script must print), so everything around it is closed: a fresh window per render
 * in an in-memory session, no Node, no preload, no new windows or navigation away,
 * no downloads, permissions or dialogs, and only the local files and network the
 * caller allowed. Each refused request is reported so the agent can see why a
 * picture or font is missing.
 */

import { BrowserWindow, session, type WebContents } from 'electron';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { writeFileAtomic } from '../atomicFile';
import { LocalBackendRequestError } from '../localBackend/client';
import { keepWindowOnItsDocument } from '../security/windowOpenPolicy';

/** The local files a page may load: under a file root, and not in private app storage. */
export type FileAccessScope = {
  fileRoots: string[];
  privateRoots: string[];
  privateRootExceptions: string[];
};

/** One page to print and what it may load. */
export type HtmlRenderInput = FileAccessScope & {
  /** Realpath of the page, already authorized by the caller. */
  htmlPath: string;
  allowNetwork: boolean;
};

export type HtmlRenderRequest = HtmlRenderInput & {
  renderRequestId: string;
  actionId: string;
  processId: string;
  toolRequestId: string;
};

export type BlockedRequestReason = 'outside_read_scope' | 'network_off' | 'not_allowed';

export type BlockedRequest = { reason: BlockedRequestReason; url: string };

export type HtmlRenderFailureCode =
  | 'HTML_LOAD_FAILED'
  | 'HTML_RENDER_TIMED_OUT'
  | 'HTML_RENDERER_CRASHED'
  | 'HTML_PRINT_FAILED'
  | 'HTML_PDF_TOO_LARGE';

export type HtmlRenderAnswer =
  | { status: 'rendered'; byte_size: number; sha256: string; blocked_requests: BlockedRequest[] }
  | {
      status: 'failed';
      code: HtmlRenderFailureCode;
      message: string;
      blocked_requests: BlockedRequest[];
    };

/** The answer, with the PDF when one was printed. */
export type HtmlRenderOutcome =
  | { answer: Extract<HtmlRenderAnswer, { status: 'rendered' }>; pdf: Buffer }
  | { answer: Extract<HtmlRenderAnswer, { status: 'failed' }>; pdf: null };

/** Whether two paths name the same existing directory. */
export type SameDirectory = (left: string, right: string) => boolean;

/** The runtime waits 30 s for the answer; this leaves it time to read the PDF. */
const RENDER_BUDGET_MS = 20_000;
const MAX_PDF_BYTES = 100 * 1024 * 1024;
const MAX_BLOCKED_REPORTED = 10;
/** No `persist:` prefix: the session lives in memory only. */
const RENDER_PARTITION = 'html-render';
/** The id becomes a file name, so it may not carry a separator or a dot segment. */
const RENDER_REQUEST_ID = /^[A-Za-z0-9_-]{1,128}$/;
const LOCAL_SCHEMES = new Set(['file:', 'data:', 'blob:', 'about:']);
/**
 * With the network off, every connection Chromium makes outside `webRequest` (WebRTC
 * over TCP, preconnects) goes to a proxy nothing listens on, loopback included.
 */
const DEAD_PROXY = { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' };
/** The types a deck loads; anything else is served without one and sniffed. */
const CONTENT_TYPES: Readonly<Record<string, string>> = {
  '.html': 'text/html',
  '.htm': 'text/html',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
};

/**
 * Every local document is sandboxed without `allow-modals`, so `print()` and the
 * other dialogs do nothing. The flags pass to every frame below it, including one
 * Chromium runs in its own renderer (a sandboxed iframe), where no script injected
 * from here would reach: `window.print()` there opens the system print panel even
 * for a hidden window. Same-origin keeps local fonts and data loadable.
 */
const DOCUMENT_SANDBOX = 'sandbox allow-scripts allow-same-origin';

/**
 * Mirror of the runtime's `is_within_any` for one root: lexically below it, or below
 * an ancestor that is the same directory under another letter case (APFS).
 */
export function isWithinRoot(target: string, root: string, sameDirectory: SameDirectory): boolean {
  const prefix = root.endsWith(path.sep) ? root : root + path.sep;
  if (target === root || target.startsWith(prefix)) return true;
  const rootName = path.basename(root).toLowerCase();
  for (let ancestor = target; path.dirname(ancestor) !== ancestor; ) {
    if (path.basename(ancestor).toLowerCase() === rootName && sameDirectory(ancestor, root)) {
      return true;
    }
    ancestor = path.dirname(ancestor);
  }
  return false;
}

/**
 * Whether the page may load this resolved local file: under a file root, and not in
 * private app storage unless under one of its exceptions (the runtime's
 * `PrivateAppStorage.hides()`). Null allows it.
 */
export function decideFileAccess(
  realPath: string,
  scope: FileAccessScope,
  sameDirectory: SameDirectory
): BlockedRequestReason | null {
  const within = (roots: string[]) =>
    roots.some((root) => isWithinRoot(realPath, root, sameDirectory));
  if (!within(scope.fileRoots)) return 'outside_read_scope';
  if (within(scope.privateRoots) && !within(scope.privateRootExceptions)) {
    return 'outside_read_scope';
  }
  return null;
}

/**
 * Whether a request may leave the machine. Local schemes are not network (file is
 * judged by `decideFileAccess`); with the network allowed only reads over http(s)
 * go out, and never as a frame: a remote page embedded in the deck runs its own
 * code in its own renderer and is no resource of the deck. Null allows it.
 */
export function decideNetworkRequest(
  request: { url: string; method: string; resourceType?: string },
  allowNetwork: boolean
): BlockedRequestReason | null {
  let protocol: string;
  try {
    protocol = new URL(request.url).protocol;
  } catch {
    return 'not_allowed';
  }
  if (LOCAL_SCHEMES.has(protocol)) return null;
  if (!allowNetwork) return 'network_off';
  const isWeb = protocol === 'http:' || protocol === 'https:';
  const isRead = request.method === 'GET' || request.method === 'HEAD';
  return isWeb && isRead && request.resourceType !== 'subFrame' ? null : 'not_allowed';
}

/** Where the PDF goes; the runtime derives the same path from the same id. */
export function htmlRenderPdfPath(localArtifactRoot: string, renderRequestId: string): string {
  if (!RENDER_REQUEST_ID.test(renderRequestId)) {
    throw new Error('Refusing an HTML render request id that cannot name a file.');
  }
  return path.join(localArtifactRoot, '.tmp', 'html-renders', `${renderRequestId}.pdf`);
}

class RenderFailure extends Error {
  constructor(
    readonly code: HtmlRenderFailureCode,
    message: string
  ) {
    super(message);
  }
}

export function sameDirectoryOnDisk(left: string, right: string): boolean {
  const a = fs.statSync(left, { throwIfNoEntry: false });
  const b = fs.statSync(right, { throwIfNoEntry: false });
  return (
    a !== undefined &&
    b !== undefined &&
    a.isDirectory() &&
    b.isDirectory() &&
    a.dev === b.dev &&
    a.ino === b.ino
  );
}

/**
 * Serves an allowed local file from the descriptor that was checked, so a link
 * swapped after the check cannot serve another file. A file that does not exist
 * fails as Chromium's loader would; a directory is never listed.
 */
async function serveFile(
  url: string,
  scope: FileAccessScope,
  block: (reason: BlockedRequestReason, url: string) => void
): Promise<Response> {
  const requestedPath = fileURLToPath(url);
  let file: fs.promises.FileHandle;
  try {
    // Non-blocking, so a FIFO cannot hold the open.
    file = await fs.promises.open(requestedPath, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
  } catch {
    return Response.error();
  }
  try {
    const opened = await file.stat();
    const realPath = await fs.promises.realpath(requestedPath);
    const reason = decideFileAccess(realPath, scope, sameDirectoryOnDisk);
    const checked = await fs.promises.stat(realPath);
    if (reason !== null || checked.dev !== opened.dev || checked.ino !== opened.ino) {
      block(reason ?? 'outside_read_scope', url);
      return Response.error();
    }
    if (!opened.isFile()) {
      block('not_allowed', url);
      return Response.error();
    }
    const contentType = CONTENT_TYPES[path.extname(realPath).toLowerCase()];
    return new Response(await file.readFile(), {
      headers: {
        'Content-Security-Policy': DOCUMENT_SANDBOX,
        ...(contentType === undefined ? {} : { 'Content-Type': contentType }),
      },
    });
  } finally {
    await file.close();
  }
}

function createRenderWindow(): BrowserWindow {
  return new BrowserWindow({
    show: false,
    skipTaskbar: true,
    focusable: false,
    webPreferences: {
      partition: RENDER_PARTITION,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      webviewTag: false,
      devTools: false,
      disableDialogs: true,
      webSecurity: true,
      backgroundThrottling: false,
      javascript: true,
    },
  });
}

async function loadAndPrint(contents: WebContents, htmlPath: string): Promise<Buffer> {
  try {
    await contents.loadURL(pathToFileURL(htmlPath).href);
    // A web font is fetched only once text uses it, which can be after `load`.
    await contents.executeJavaScript('document.fonts.ready.then(() => undefined)');
  } catch (error) {
    throw new RenderFailure('HTML_LOAD_FAILED', `The page did not load: ${String(error)}`);
  }
  try {
    return await contents.printToPDF({
      printBackground: true,
      preferCSSPageSize: true,
      pageSize: 'A4',
    });
  } catch (error) {
    throw new RenderFailure('HTML_PRINT_FAILED', `The page could not be printed: ${String(error)}`);
  }
}

/** Settles with the first of the work, the crash of its renderer, or the budget. */
function withinBudget(contents: WebContents, work: Promise<Buffer>): Promise<Buffer> {
  let timer: NodeJS.Timeout | undefined;
  const stopped = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(
      () =>
        reject(
          new RenderFailure(
            'HTML_RENDER_TIMED_OUT',
            `The page did not finish printing within ${RENDER_BUDGET_MS / 1000} s.`
          )
        ),
      RENDER_BUDGET_MS
    );
    contents.once('render-process-gone', (_event, details) =>
      reject(new RenderFailure('HTML_RENDERER_CRASHED', `The page renderer ${details.reason}.`))
    );
  });
  // The losing side settles after the window is gone; nothing waits for it.
  work.catch(() => undefined);
  return Promise.race([work, stopped]).finally(() => clearTimeout(timer));
}

async function renderHtml(request: HtmlRenderInput): Promise<HtmlRenderOutcome> {
  const blocked: BlockedRequest[] = [];
  const block = (reason: BlockedRequestReason, url: string) => {
    if (blocked.length < MAX_BLOCKED_REPORTED) blocked.push({ reason, url });
  };
  const ses = session.fromPartition(RENDER_PARTITION);
  ses.protocol.handle('file', (req) => serveFile(req.url, request, block));
  ses.webRequest.onBeforeRequest((details, callback) => {
    const reason = decideNetworkRequest(details, request.allowNetwork);
    if (reason !== null) block(reason, details.url);
    callback({ cancel: reason !== null });
  });
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
  const refuseDownload = (event: Electron.Event) => event.preventDefault();
  ses.on('will-download', refuseDownload);

  const window = createRenderWindow();
  try {
    const contents = window.webContents;
    if (!request.allowNetwork) {
      await ses.setProxy(DEAD_PROXY);
      contents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
    }
    // Replaces the app-wide handler, which would open web links in the browser.
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    keepWindowOnItsDocument(contents);
    contents.on('will-attach-webview', (event) => event.preventDefault());
    const pdf = await withinBudget(contents, loadAndPrint(contents, request.htmlPath));
    if (pdf.byteLength > MAX_PDF_BYTES) {
      throw new RenderFailure('HTML_PDF_TOO_LARGE', 'The printed PDF is larger than 100 MB.');
    }
    const sha256 = createHash('sha256').update(pdf).digest('hex');
    return {
      answer: { status: 'rendered', byte_size: pdf.byteLength, sha256, blocked_requests: blocked },
      pdf,
    };
  } catch (error) {
    if (!(error instanceof RenderFailure)) throw error;
    return {
      answer: {
        status: 'failed',
        code: error.code,
        message: error.message,
        blocked_requests: blocked,
      },
      pdf: null,
    };
  } finally {
    window.destroy();
    ses.protocol.unhandle('file');
    ses.webRequest.onBeforeRequest(null);
    ses.removeListener('will-download', refuseDownload);
    await Promise.all([ses.setProxy({ mode: 'system' }), ses.clearCache(), ses.clearStorageData()]);
  }
}

/** Every render shares the partition's session and its one `file` handler. */
let renderQueue: Promise<unknown> = Promise.resolve();

/** Prints one page after every render asked for before it has finished. */
export function renderHtmlToPdf(input: HtmlRenderInput): Promise<HtmlRenderOutcome> {
  const rendered = renderQueue.then(() => renderHtml(input));
  // The caller gets the failure; the next render still runs.
  renderQueue = rendered.catch(() => undefined);
  return rendered;
}

/**
 * Answers `html_render_requested`.
 *
 * An unexpected failure posts nothing; the waiting tool times out and reports that
 * no PDF was made, rather than an invented reason.
 */
export function createHtmlRenderResponder(params: {
  localArtifactRoot: string;
  postAnswer: (body: { render_request_id: string; result: HtmlRenderAnswer }) => Promise<unknown>;
  logger?: { error?: (name: string, payload?: unknown) => void } | null;
}): (request: HtmlRenderRequest) => Promise<void> {
  return async (request) => {
    try {
      const pdfPath = htmlRenderPdfPath(params.localArtifactRoot, request.renderRequestId);
      const { answer, pdf } = await renderHtmlToPdf(request);
      if (pdf !== null) writeFileAtomic(pdfPath, path.dirname(pdfPath), pdf);
      // Sent once, never retried: the runtime takes the first answer and reads the
      // PDF after it, so only a 404 (the tool already gave up) leaves it unread.
      try {
        await params.postAnswer({ render_request_id: request.renderRequestId, result: answer });
      } catch (error) {
        if (error instanceof LocalBackendRequestError && error.status === 404) {
          fs.rmSync(pdfPath, { force: true });
        }
        throw error;
      }
    } catch (error) {
      params.logger?.error?.('HTML_RENDER_REQUEST_ERR', {
        render_request_id: request.renderRequestId,
        err: error,
      });
    }
  };
}
