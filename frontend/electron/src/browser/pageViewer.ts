/**
 * Shows the user, view only, the app's hidden page an Action works on. The main window asks to
 * watch an Action; main picks the page, the one that Action touched last among its live pages
 * (each runner process, a subagent's included, has a page of its own), and streams it to that
 * window as CDP screencast frames, following the Action as it moves to another page.
 *
 * The viewer never enters a session's operation queue: it sends its own commands on the page's
 * debugger beside the operations, and a failed command ends the watch, never an operation. The
 * screencast changes nothing of the page: no viewport, emulation or focus, and frames are scaled
 * only through `maxWidth` / `maxHeight`. Frames live in memory on their way to the window that
 * asked; nothing writes or logs them. Nothing goes the other way: there is no input.
 *
 * A cross-process navigation needs no restart: Chromium's DevTools page handler moves a running
 * screencast to the new renderer's frames.
 */

import { randomUUID } from 'node:crypto';

import type { Debugger, WebContents } from 'electron';

import { PAGE_VIEWPORT } from './pageWindowOptions';

export type BrowserViewEndReason = 'work_ended' | 'page_crashed';

export type BrowserViewUpdate =
  // `jpeg` is base64; width and height are the page's viewport in CSS pixels.
  | { kind: 'frame'; watchId: string; jpeg: string; width: number; height: number }
  | { kind: 'page'; watchId: string; url: string; title: string }
  | { kind: 'ended'; watchId: string; reason: BrowserViewEndReason };

export type BrowserViewWatchResult = { kind: 'watching'; watchId: string } | { kind: 'not_live' };

/** A session's page as the viewer sees it; `ready` settles once its debugger is attached. */
export type ViewedPage = { contents: WebContents; cdp: Debugger; ready: Promise<void> };

/** What the browser sessions tell the viewer about their pages. */
export type AppPageEvents = {
  /** `actionId` is null for a run with no Action, whose page is never shown. */
  opened(sessionId: string, actionId: string | null, page: ViewedPage): void;
  /** An operation for the session arrived. */
  touched(sessionId: string): void;
  closed(sessionId: string, crashed: boolean): void;
};

/** The window that watches. */
export type BrowserViewer = Pick<WebContents, 'id' | 'send' | 'isDestroyed'> & {
  once(event: 'destroyed', listener: () => void): unknown;
  removeListener(event: 'destroyed', listener: () => void): unknown;
};

type LoggerLike = { error?: (name: string, payload?: unknown) => void };

type TrackedPage = {
  actionId: string | null;
  page: ViewedPage;
  /** When an operation last arrived, as a count of arrivals; the highest is the Action's page. */
  touched: number;
  /** Takes the page out of the Action's live state; null for a page of no Action. */
  endLive: (() => void) | null;
};

type Watch = {
  id: string;
  actionId: string;
  viewer: BrowserViewer;
  /** The page shown now; replaced on each move, so a late answer for an earlier one is ignored. */
  target: { sessionId: string; detach: (stopCast: boolean) => void } | null;
  onViewerGone: () => void;
};

type ScreencastFrame = {
  data: string;
  sessionId: number;
  metadata: { deviceWidth: number; deviceHeight: number };
};

const UPDATE_CHANNEL = 'browserView:update';
// No larger than the page's own viewport, which the page renders at device scale 1.
const SCREENCAST = {
  format: 'jpeg',
  maxWidth: PAGE_VIEWPORT.width,
  maxHeight: PAGE_VIEWPORT.height,
} as const;

/** A log names an error only by its class: its message can carry page text or a private URL. */
const errorName = (error: unknown) => (error instanceof Error ? error.name : typeof error);

export type AppPageViewer = ReturnType<typeof createAppPageViewer>;

export function createAppPageViewer(params: {
  /** Shows in the Action's live state that it has a live app page, until the closer runs. */
  beginLivePage: (actionId: string) => () => void;
  logger?: LoggerLike | null;
}) {
  const pages = new Map<string, TrackedPage>();
  // One watch per window, the main window's: only it may watch, so no two watches share a page's
  // screencast.
  const watches = new Map<number, Watch>();
  let arrivals = 0;

  /** The live page the Action touched last. */
  function currentPage(actionId: string): [string, TrackedPage] | null {
    let found: [string, TrackedPage] | null = null;
    for (const entry of pages) {
      if (entry[1].actionId === actionId && (found === null || entry[1].touched > found[1].touched))
        found = entry;
    }
    return found;
  }

  function send(watch: Watch, update: BrowserViewUpdate): void {
    if (!watch.viewer.isDestroyed()) watch.viewer.send(UPDATE_CHANNEL, update);
  }

  function end(watch: Watch, reason: BrowserViewEndReason | null, stopCast: boolean): void {
    if (watches.get(watch.viewer.id) !== watch) return;
    watches.delete(watch.viewer.id);
    watch.target?.detach(stopCast);
    watch.target = null;
    watch.viewer.removeListener('destroyed', watch.onViewerGone);
    if (reason !== null) send(watch, { kind: 'ended', watchId: watch.id, reason });
  }

  /** Shows `tracked` in the watch from now on. */
  function attach(watch: Watch, sessionId: string, tracked: TrackedPage): void {
    const { contents, cdp, ready } = tracked.page;
    const showPage = () =>
      send(watch, {
        kind: 'page',
        watchId: watch.id,
        url: contents.getURL(),
        title: contents.getTitle(),
      });
    const isShown = () => watches.get(watch.viewer.id) === watch && watch.target === target;
    const failed = (error: unknown) => {
      if (!isShown()) return;
      params.logger?.error?.('BROWSER_VIEW_SCREENCAST_ERR', { error: errorName(error) });
      end(watch, 'work_ended', true);
    };
    const onMessage = (_event: unknown, method: string, data: unknown, cdpSession?: string) => {
      // A frame target's events name it; the screencast is the page's own.
      if (method !== 'Page.screencastFrame' || cdpSession !== undefined) return;
      const frame = data as ScreencastFrame;
      send(watch, {
        kind: 'frame',
        watchId: watch.id,
        jpeg: frame.data,
        width: frame.metadata.deviceWidth,
        height: frame.metadata.deviceHeight,
      });
      // Chromium sends the next frame only once this one is acknowledged.
      cdp.sendCommand('Page.screencastFrameAck', { sessionId: frame.sessionId }).catch(failed);
    };
    const target: NonNullable<Watch['target']> = {
      sessionId,
      detach: (stopCast) => {
        cdp.removeListener('message', onMessage);
        contents.removeListener('did-navigate', showPage);
        contents.removeListener('did-navigate-in-page', showPage);
        contents.removeListener('page-title-updated', showPage);
        // A closed page has no screencast left to stop; one closing meanwhile stops its own.
        if (stopCast) cdp.sendCommand('Page.stopScreencast').catch(() => undefined);
      },
    };
    watch.target = target;
    cdp.on('message', onMessage);
    contents.on('did-navigate', showPage);
    contents.on('did-navigate-in-page', showPage);
    contents.on('page-title-updated', showPage);
    ready
      .then(async () => {
        if (!isShown()) return;
        await cdp.sendCommand('Page.startScreencast', SCREENCAST);
        // After the window has the watch id: the start is a round trip to the page's renderer.
        if (isShown()) showPage();
      })
      .catch(failed);
  }

  /** Moves the watch to the Action's current page, or ends it when the Action has none. */
  function retarget(watch: Watch, stopCast: boolean): void {
    const next = currentPage(watch.actionId);
    if (next === null) {
      end(watch, 'work_ended', stopCast);
      return;
    }
    if (watch.target?.sessionId === next[0]) return;
    watch.target?.detach(stopCast);
    attach(watch, next[0], next[1]);
  }

  const events: AppPageEvents = {
    opened(sessionId, actionId, page) {
      pages.set(sessionId, {
        actionId,
        page,
        touched: 0,
        endLive: actionId === null ? null : params.beginLivePage(actionId),
      });
    },
    touched(sessionId) {
      const tracked = pages.get(sessionId);
      if (tracked === undefined) return;
      tracked.touched = ++arrivals;
      for (const watch of watches.values()) {
        if (watch.actionId === tracked.actionId) retarget(watch, true);
      }
    },
    closed(sessionId, crashed) {
      const tracked = pages.get(sessionId);
      if (tracked === undefined) return;
      pages.delete(sessionId);
      tracked.endLive?.();
      for (const watch of [...watches.values()]) {
        if (watch.target?.sessionId !== sessionId) continue;
        // The crash is what the user should see; another page of the Action is shown otherwise.
        if (crashed) end(watch, 'page_crashed', false);
        else retarget(watch, false);
      }
    },
  };

  return {
    pageEvents: events,
    /** Starts showing the Action's page in `viewer`, in place of what it watched before. */
    watch(actionId: string, viewer: BrowserViewer): BrowserViewWatchResult {
      const current = currentPage(actionId);
      if (current === null) return { kind: 'not_live' };
      const previous = watches.get(viewer.id);
      if (previous !== undefined) end(previous, null, true);
      const watch: Watch = {
        id: randomUUID(),
        actionId,
        viewer,
        target: null,
        onViewerGone: () => end(watch, null, true),
      };
      watches.set(viewer.id, watch);
      viewer.once('destroyed', watch.onViewerGone);
      attach(watch, current[0], current[1]);
      return { kind: 'watching', watchId: watch.id };
    },
    /** Idempotent; a watch id the window does not hold changes nothing. */
    unwatch(watchId: string, viewerId: number): void {
      const watch = watches.get(viewerId);
      if (watch?.id === watchId) end(watch, null, true);
    },
  };
}
