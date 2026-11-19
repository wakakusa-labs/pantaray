/**
 * Answers `browser_operation_requested`: runs the operation in its session and posts the result
 * to `POST /local/action-browser`, with the screenshot or PDF written where the runtime reads it.
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
  type BrowserPage,
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
import { takeSnapshot } from './snapshot';

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

async function navigate(
  contents: WebContents,
  target: Extract<BrowserOperation, { action: 'navigate' }>['target']
): Promise<void> {
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

async function perform(operation: BrowserOperation, page: BrowserPage): Promise<OperationValue> {
  switch (operation.action) {
    case 'navigate':
      await navigate(page.contents, operation.target);
      return NOTHING;
    case 'back':
      await goBack(page.contents);
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
      return { ...NOTHING, artifact: await printPage(page.contents) };
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
  localArtifactRoot: string;
  postAnswer: (body: BrowserOperationAnswer) => Promise<unknown>;
  logger?: LoggerLike | null;
}): (request: BrowserOperationRequest) => Promise<void> {
  return async (request) => {
    const id = request.operation_request_id;
    let written: string | null = null;
    try {
      const settled = await params.sessions.run(request, budgetOf(request.operation), (page) =>
        perform(request.operation, page)
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
