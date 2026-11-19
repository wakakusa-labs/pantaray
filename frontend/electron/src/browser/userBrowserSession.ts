/**
 * The browser sessions that run in the user's own browser, one per `session_id`: a hidden
 * background tab of the session's own (`userBrowserConnection`), signed in to the user's real
 * accounts. The app's hidden page never serves these sessions, nor they the app's.
 *
 * The first operation connects. The user allows the connection in their browser, which takes as
 * long as it takes; only closing the session ends that wait. The operation's budget starts once
 * the connection is open and covers opening and guarding the tab.
 *
 * The tab is `userBrowserTab`'s: guarded before it runs, and judged by the session's latest rules.
 * A tab that cannot be guarded fails the operation and ends the session.
 *
 * Operations run one after another. Past its budget the operation's tab is closed and the
 * connection kept, so the next `navigate` opens a new tab without asking the user again. A
 * session ends on close, when the browser drops the connection, or when its tab cannot be
 * guarded; its id is refused from then on.
 */

import type { UserBrowserId } from './browserContract';
import {
  BrowserOperationError,
  type OperationSettled,
  type PageReport,
  type SessionRequest,
} from './browserSession';
import { KNOWN_BROWSERS } from './knownBrowsers';
import type { UserBrowserConnection } from './userBrowserConnection';
import {
  openGuardedTab,
  type GuardedTab,
  type TabContext,
  type UserBrowserPage,
} from './userBrowserTab';

export type { UserBrowserPage } from './userBrowserTab';

/** What the request names of the user's browser: which one, and the hosts the tab may open. */
export type UserBrowserTarget = { browser: UserBrowserId; read_hosts: string[] };

type LoggerLike = { error?: (name: string, payload?: unknown) => void };

type UserSession = TabContext & {
  id: string;
  browser: UserBrowserId;
  abort: AbortController;
  connection: Promise<UserBrowserConnection>;
  /** The tab operations run in; null before the first `navigate` and after a timeout. */
  tab: Promise<GuardedTab> | null;
  queue: Promise<void>;
  end: BrowserOperationError | null;
  ended: Promise<never>;
  stop: (reason: BrowserOperationError) => void;
};

const sessionGone = () =>
  new BrowserOperationError('BROWSER_SESSION_GONE', 'This browser session was closed.');
const noPage = () =>
  new BrowserOperationError('BROWSER_NO_PAGE', 'No page is open in this session.');
const unavailable = (browser: UserBrowserId) =>
  new BrowserOperationError(
    'BROWSER_USER_UNAVAILABLE',
    `${KNOWN_BROWSERS[browser].name} could not be used.`
  );

/** A log names an error only by its class: its message can carry page text or a private URL. */
function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

function drain(session: UserSession): PageReport {
  const report = {
    notes: [...session.notes],
    popups: session.popups,
    blocked: session.blocked,
    network: session.unreported,
  };
  session.notes = new Set();
  session.popups = [];
  session.blocked = [];
  session.unreported = [];
  return report;
}

export type UserBrowserSessions = ReturnType<typeof createUserBrowserSessions>;

export function createUserBrowserSessions(params: {
  /** Connects to the user's browser; rejects with `signal.reason` once `signal` aborts. */
  connect: (browser: UserBrowserId, signal: AbortSignal) => Promise<UserBrowserConnection>;
  /** The backend's own port, which the tab may not reach under any host name. */
  getBackendPort: () => number;
  logger?: LoggerLike | null;
}) {
  const sessions = new Map<string, UserSession>();
  /** Ids closed or ended; the runtime mints a new id for the run's next session. */
  const ended = new Set<string>();

  /** The tab goes with the connection, so a tab that cannot be closed first is gone too. */
  async function release(session: UserSession): Promise<void> {
    let connection: UserBrowserConnection;
    try {
      connection = await session.connection;
    } catch {
      return; // Refused or aborted: nothing was opened.
    }
    await session.tab?.then((tab) => tab.handle.close()).catch(() => undefined);
    connection.close();
  }

  function discard(session: UserSession, reason: BrowserOperationError): void {
    if (session.end !== null) return;
    session.end = reason;
    ended.add(session.id);
    sessions.delete(session.id);
    session.stop(reason);
    session.abort.abort(reason);
    void release(session);
  }

  function open(id: string, browser: UserBrowserId): UserSession {
    const abort = new AbortController();
    let stop: UserSession['stop'] = () => undefined;
    const endedPromise = new Promise<never>((_resolve, reject) => (stop = reject));
    endedPromise.catch(() => undefined);
    const session: UserSession = {
      id,
      browser,
      abort,
      connection: params.connect(browser, abort.signal),
      tab: null,
      backendPort: params.getBackendPort(),
      readHosts: [],
      writeHosts: [],
      contacted: [],
      unreported: [],
      notes: new Set(),
      popups: [],
      blocked: [],
      queue: Promise.resolve(),
      end: null,
      ended: endedPromise,
      stop,
    };
    session.connection.then(
      (connection) => connection.closed.then(() => discard(session, unavailable(browser))),
      () => undefined
    );
    sessions.set(id, session);
    return session;
  }

  async function execute<T>(
    session: UserSession,
    request: SessionRequest,
    target: UserBrowserTarget,
    budgetMs: number,
    work: (page: UserBrowserPage) => Promise<T>
  ): Promise<OperationSettled<T>> {
    const failed = (error: BrowserOperationError): OperationSettled<T> => ({
      status: 'failed',
      code: error.code,
      message: error.message,
      network: drain(session).network,
    });
    if (session.end !== null) return failed(session.end);
    session.readHosts = target.read_hosts;
    session.writeHosts = request.policy.write_hosts;
    let connection: UserBrowserConnection;
    try {
      // No budget: the user allows the connection in their browser in their own time.
      connection = await Promise.race([session.connection, session.ended]);
    } catch (error) {
      if (session.end !== null) return failed(session.end);
      const reason = error instanceof BrowserOperationError ? error : unavailable(session.browser);
      discard(session, reason);
      return failed(reason);
    }
    if (session.tab === null) {
      if (request.operation.action !== 'navigate') return failed(noPage());
      const { browser } = session;
      session.tab = openGuardedTab(session, connection, params.logger).catch(() => {
        throw unavailable(browser);
      });
      session.tab.catch(() => undefined);
    }
    const opening = session.tab;
    let timer: NodeJS.Timeout | undefined;
    const budget = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(
        () =>
          reject(
            new BrowserOperationError(
              'BROWSER_TIMED_OUT',
              `The page did not finish within ${budgetMs / 1000} s, so its tab was closed.`
            )
          ),
        budgetMs
      );
    });
    const job = (async () => {
      const tab = await opening;
      const value = await work(tab.page);
      return { value, ...(await tab.location()) };
    })();
    // The losing side settles after the answer; nothing waits for it.
    job.catch(() => undefined);
    try {
      const { value, url, title } = await Promise.race([job, budget, session.ended]);
      return { status: 'ok', value, url, title, report: drain(session) };
    } catch (error) {
      if (session.end !== null) return failed(session.end);
      if (error instanceof BrowserOperationError) {
        if (error.code === 'BROWSER_USER_UNAVAILABLE') discard(session, error);
        // The work past its budget may still act on the tab, so no later operation may share it.
        if (error.code === 'BROWSER_TIMED_OUT' && session.tab === opening) {
          session.tab = null;
          void opening.then((tab) => tab.handle.close()).catch(() => undefined);
        }
        return failed(error);
      }
      params.logger?.error?.('USER_BROWSER_OPERATION_ERR', {
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
    const session = sessions.get(sessionId);
    if (session !== undefined) discard(session, sessionGone());
  }

  return {
    /**
     * Runs `work` in the session's tab, after every operation sent to it before; the first
     * `navigate` naming a new id connects to `target.browser`.
     */
    run<T>(
      request: SessionRequest,
      target: UserBrowserTarget,
      budgetMs: number,
      work: (page: UserBrowserPage) => Promise<T>
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
      let session = sessions.get(id);
      if (session === undefined) {
        if (request.operation.action !== 'navigate') return refuse(noPage());
        session = open(id, target.browser);
      }
      const opened = session;
      const settled = opened.queue.then(() => execute(opened, request, target, budgetMs, work));
      opened.queue = settled.then(
        () => undefined,
        () => undefined
      );
      return settled;
    },
    /** Idempotent; aborts a connection still waiting for the user, and closes the tab. */
    close,
    closeAll(): void {
      for (const id of [...sessions.keys()]) close(id);
    },
  };
}
