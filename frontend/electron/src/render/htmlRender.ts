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
 * picture or font is missing, and each host the page was let to reach is reported so
 * the user can see where it went.
 */

import { BrowserWindow, session, type WebContents } from 'electron';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { writeFileAtomic } from '../atomicFile';
import {
  LOCAL_SCHEMES,
  reachedHost,
  serveFile,
  type ContactedHost,
  type FileAccessScope,
} from '../browser/pagePolicy';
import { LocalBackendRequestError } from '../localBackend/client';
import { keepWindowOnItsDocument } from '../security/windowOpenPolicy';

// The task preview takes its file rules from here.
export {
  isWithinRoot,
  sameDirectoryOnDisk,
  type FileAccessScope,
  type SameDirectory,
} from '../browser/pagePolicy';

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

export type BlockedRequestReason =
  | 'outside_read_scope'
  | 'network_off'
  | 'not_allowed'
  | 'too_many_hosts';

export type BlockedRequest = { reason: BlockedRequestReason; url: string };

export type HtmlRenderFailureCode =
  | 'HTML_LOAD_FAILED'
  | 'HTML_RENDER_TIMED_OUT'
  | 'HTML_RENDERER_CRASHED'
  | 'HTML_PRINT_FAILED'
  | 'HTML_PDF_TOO_LARGE';

export type HtmlRenderAnswer =
  | {
      status: 'rendered';
      byte_size: number;
      sha256: string;
      blocked_requests: BlockedRequest[];
      network: ContactedHost[];
    }
  | {
      status: 'failed';
      code: HtmlRenderFailureCode;
      message: string;
      blocked_requests: BlockedRequest[];
      network: ContactedHost[];
    };

/** The answer, with the PDF when one was printed. */
export type HtmlRenderOutcome =
  | { answer: Extract<HtmlRenderAnswer, { status: 'rendered' }>; pdf: Buffer }
  | { answer: Extract<HtmlRenderAnswer, { status: 'failed' }>; pdf: null };

/** The runtime waits 30 s for the answer; this leaves it time to read the PDF. */
const RENDER_BUDGET_MS = 20_000;
const MAX_PDF_BYTES = 100 * 1024 * 1024;
const MAX_BLOCKED_REPORTED = 10;
/**
 * The runtime accepts this many; a deck reaches a handful (fonts, a chart library). Past it
 * a request to a new host is refused, so every host a page reaches is reported.
 */
const MAX_HOSTS_REPORTED = 50;
/** No `persist:` prefix: the session lives in memory only. */
const RENDER_PARTITION = 'html-render';
/** The id becomes a file name, so it may not carry a separator or a dot segment. */
const RENDER_REQUEST_ID = /^[A-Za-z0-9_-]{1,128}$/;
/**
 * With the network off, every connection Chromium makes outside `webRequest` (WebRTC
 * over TCP, preconnects) goes to a proxy nothing listens on, loopback included.
 */
const DEAD_PROXY = { proxyRules: 'http://127.0.0.1:9', proxyBypassRules: '<-loopback>' };

/**
 * Whether a request may leave the machine. Local schemes are not network (file is
 * judged by `serveFile`); with the network allowed only reads over http(s)
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
  const network = new Map<string, ContactedHost>();
  /** Records an allowed URL's host; a local scheme reaches none. Refuses a host past the cap. */
  const reach = (url: string): BlockedRequestReason | null => {
    const contacted = reachedHost(new URL(url));
    if (contacted === null) return null;
    const key = `${contacted.host}:${contacted.port}`;
    if (!network.has(key) && network.size >= MAX_HOSTS_REPORTED) return 'too_many_hosts';
    network.set(key, contacted);
    return null;
  };
  const ses = session.fromPartition(RENDER_PARTITION);
  ses.protocol.handle('file', (req) => serveFile(req.url, request, block));
  ses.webRequest.onBeforeRequest((details, callback) => {
    const reason = decideNetworkRequest(details, request.allowNetwork) ?? reach(details.url);
    if (reason !== null) block(reason, details.url);
    callback({ cancel: reason !== null });
  });
  // A resource hint (`<link rel="preconnect">`) opens a connection without passing
  // webRequest, and Electron has no way to refuse it. It sends no request, only the host
  // name; the request that follows passes webRequest. Past the cap it is not recorded.
  const recordPreconnect = (_event: Electron.Event, url: string) => {
    if (request.allowNetwork) reach(url);
  };
  ses.on('preconnect', recordPreconnect);
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
      answer: {
        status: 'rendered',
        byte_size: pdf.byteLength,
        sha256,
        blocked_requests: blocked,
        network: [...network.values()],
      },
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
        network: [...network.values()],
      },
      pdf: null,
    };
  } finally {
    window.destroy();
    ses.protocol.unhandle('file');
    ses.webRequest.onBeforeRequest(null);
    ses.removeListener('will-download', refuseDownload);
    ses.removeListener('preconnect', recordPreconnect);
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
