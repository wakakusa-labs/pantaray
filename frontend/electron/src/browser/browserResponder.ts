/**
 * Answers `browser_operation_requested`: runs the operation in its session, the app's hidden page
 * or, when the request names `user_browser`, a tab of the session's own in the user's browser,
 * and posts the result to `POST /local/action-browser`, with the screenshot or PDF written where
 * the runtime reads it.
 */

import type { WebContents } from 'electron';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

import { LocalBackendRequestError } from '../localBackend/client';
import type {
  BrowserArtifactClaim,
  BrowserOperation,
  BrowserOperationAnswer,
  BrowserOperationRequest,
  BrowserPageData,
} from './browserContract';
import {
  BROWSER_NAVIGATION_BUDGET_MS,
  BROWSER_OPERATION_BUDGET_MS,
  BrowserOperationError,
  type BrowserSessions,
} from './browserSession';
import {
  browserArtifactPath,
  captureFullPage,
  captureViewport,
  printPage,
  writeArtifact,
  type CapturedArtifact,
} from './capture';
import { captureElement, interact } from './interactions';
import { reachedHost } from './pagePolicy';
import type { CdpPage } from './cdpSession';
import { takeSnapshot } from './snapshot';
import type { UserBrowserPage, UserBrowserSessions } from './userBrowserSession';

type OkResult = Extract<BrowserOperationAnswer['result'], { status: 'ok' }>;

/** What an operation produced beyond the page's url and title. */
type OperationValue = {
  artifact: CapturedArtifact | null;
  truncated: OkResult['truncated'];
  snapshot: BrowserPageData['snapshot'];
  console: BrowserPageData['console'];
};

const NOTHING: OperationValue = { artifact: null, truncated: null, snapshot: null, console: null };
/** Chromium's code for a load that another navigation replaced, which is no failure of the page. */
const ERR_ABORTED = -3;

type LoggerLike = { error?: (name: string, payload?: unknown) => void };
type NavigateTarget = Extract<BrowserOperation, { action: 'navigate' }>['target'];

/** How an operation moves the page, which differs between the app's page and the user's tab. */
type PageNavigation = {
  navigate(target: NavigateTarget): Promise<void>;
  back(): Promise<void>;
  /** Null where the page does not print: the user's browser. */
  print: (() => Promise<CapturedArtifact>) | null;
};

export async function navigate(contents: WebContents, target: NavigateTarget): Promise<void> {
  const url = target.kind === 'web' ? target.url : pathToFileURL(target.path).href;
  try {
    await contents.loadURL(url);
  } catch (error) {
    // Only Chromium's error name: the message repeats URLs a redirect may have taken from the page.
    const code = (error as { code?: unknown }).code;
    throw new BrowserOperationError(
      'BROWSER_NAVIGATION_FAILED',
      `The page did not load (${typeof code === 'string' ? code : 'unknown error'}).`
    );
  }
}

/** Goes one entry back and waits until it is shown: committed, and loaded unless it came from a cache. */
async function goBack(contents: WebContents): Promise<void> {
  if (!contents.navigationHistory.canGoBack()) {
    throw new BrowserOperationError(
      'BROWSER_ACTION_FAILED',
      'There is no earlier page to go back to.'
    );
  }
  await new Promise<void>((resolve, reject) => {
    const settle = (failure: string | null) => {
      contents.removeListener('did-navigate', onCommitted);
      contents.removeListener('did-navigate-in-page', onCommitted);
      contents.removeListener('did-stop-loading', onStopped);
      contents.removeListener('did-fail-load', onFailed);
      if (failure === null) resolve();
      else reject(new BrowserOperationError('BROWSER_NAVIGATION_FAILED', failure));
    };
    const onStopped = () => settle(null);
    const onCommitted = () => {
      if (!contents.isLoading()) settle(null);
    };
    const onFailed = (
      _event: Electron.Event,
      errorCode: number,
      errorDescription: string,
      _url: string,
      isMainFrame: boolean
    ) => {
      if (isMainFrame && errorCode !== ERR_ABORTED) {
        settle(`The earlier page did not load (${errorDescription}).`);
      }
    };
    contents.on('did-navigate', onCommitted);
    contents.on('did-navigate-in-page', onCommitted);
    contents.on('did-stop-loading', onStopped);
    contents.on('did-fail-load', onFailed);
    contents.navigationHistory.goBack();
  });
}

/**
 * The host a navigate opens, by the WHATWG parser Chromium loads the URL with, so the runtime
 * never parses it itself. Read from the request, never from the page or where a redirect led;
 * null for a file and for every other operation.
 */
export function openedHost(operation: BrowserOperation): string | null {
  if (operation.action !== 'navigate' || operation.target.kind !== 'web') return null;
  return reachedHost(new URL(operation.target.url))?.host ?? null;
}

const appNavigation = (contents: WebContents): PageNavigation => ({
  navigate: (target) => navigate(contents, target),
  back: () => goBack(contents),
  print: () => printPage(contents),
});

/** The runtime sends the user's browser no file and no print; the contract refuses both. */
const notOffered = () =>
  new BrowserOperationError('BROWSER_ACTION_FAILED', "The user's browser opens web pages only.");

const userNavigation = (page: UserBrowserPage): PageNavigation => ({
  navigate: async (target) => {
    if (target.kind !== 'web') throw notOffered();
    await page.navigate(target.url);
  },
  back: () => page.back(),
  print: null,
});

async function perform(
  operation: BrowserOperation,
  page: CdpPage,
  navigation: PageNavigation
): Promise<OperationValue> {
  switch (operation.action) {
    case 'navigate':
      await navigation.navigate(operation.target);
      return NOTHING;
    case 'back':
      await navigation.back();
      return NOTHING;
    case 'screenshot': {
      if (operation.ref == null && operation.full_page !== true) {
        return { ...NOTHING, artifact: await captureViewport(page.cdp) };
      }
      const { artifact, cut } =
        operation.ref == null
          ? await captureFullPage(page.cdp)
          : await captureElement(page, operation.ref);
      return { ...NOTHING, artifact, truncated: cut ? 'page_too_tall' : null };
    }
    case 'print':
      if (navigation.print === null) throw notOffered();
      return { ...NOTHING, artifact: await navigation.print() };
    case 'snapshot': {
      const { text, cut } = await takeSnapshot(page, operation.ref);
      return { ...NOTHING, snapshot: text, truncated: cut ? 'snapshot_too_long' : null };
    }
    case 'console':
      return {
        ...NOTHING,
        console: [...page.console.lines],
        truncated: page.console.cut ? 'console_too_long' : null,
      };
    default:
      await interact(page, operation);
      return NOTHING;
  }
}

function budgetOf(operation: BrowserOperation): number {
  // A `wait` is bounded by `BROWSER_MAX_WAIT_SECONDS`, well within the navigation budget.
  return ['navigate', 'back', 'wait'].includes(operation.action)
    ? BROWSER_NAVIGATION_BUDGET_MS
    : BROWSER_OPERATION_BUDGET_MS;
}

/**
 * An unexpected failure posts nothing; the waiting tool times out and reports that the browser
 * did not answer, rather than an invented reason.
 */
export function createBrowserResponder(params: {
  sessions: BrowserSessions;
  userSessions: UserBrowserSessions;
  localArtifactRoot: string;
  postAnswer: (body: BrowserOperationAnswer) => Promise<unknown>;
  logger?: LoggerLike | null;
}): (request: BrowserOperationRequest) => Promise<void> {
  return async (request) => {
    const id = request.operation_request_id;
    let written: string | null = null;
    try {
      const budget = budgetOf(request.operation);
      const settled =
        request.user_browser === null
          ? await params.sessions.run(request, budget, (page) =>
              perform(request.operation, page, appNavigation(page.contents))
            )
          : await params.userSessions.run(request, request.user_browser, budget, (page) =>
              perform(request.operation, page, userNavigation(page))
            );
      let result: BrowserOperationAnswer['result'];
      if (settled.status === 'failed') {
        result = settled;
      } else {
        const { artifact, truncated, snapshot, console: consoleLines } = settled.value;
        let claim: BrowserArtifactClaim | null = null;
        if (artifact !== null) {
          written = browserArtifactPath(params.localArtifactRoot, id, artifact.kind);
          claim = writeArtifact(written, artifact);
        }
        const { notes, popups, blocked, network } = settled.report;
        result = {
          status: 'ok',
          page_data: {
            url: settled.url,
            title: settled.title,
            snapshot,
            console: consoleLines,
            popups,
            blocked,
          },
          notes,
          truncated,
          network,
          artifact: claim,
          opened_host: openedHost(request.operation),
        };
      }
      // Sent once, never retried: the runtime takes the first answer and reads the artifact
      // after it, so only a 404 (the tool already gave up) leaves it unread.
      try {
        await params.postAnswer({ operation_request_id: id, result });
      } catch (error) {
        if (written !== null && error instanceof LocalBackendRequestError && error.status === 404) {
          fs.rmSync(written, { force: true });
        }
        throw error;
      }
    } catch (error) {
      // Only the error's class and status: its message can carry page text or a private URL.
      params.logger?.error?.('BROWSER_OPERATION_REQUEST_ERR', {
        operation_request_id: id,
        error: error instanceof Error ? error.name : typeof error,
        status: error instanceof LocalBackendRequestError ? error.status : null,
      });
    }
  };
}
