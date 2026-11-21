/**
 * The browsers main runs for the runtime's operations: the app's hidden pages, with the viewer
 * that shows an Action's page to the user, and tabs in the user's own browser. A session id is
 * one or the other, so a close goes to both.
 */

import { createBrowserResponder } from '../browser/browserResponder';
import type { BrowserOperationAnswer } from '../browser/browserContract';
import { createBrowserSessions } from '../browser/browserSession';
import { createAppPageViewer } from '../browser/pageViewer';
import { connectUserBrowser, runCommand } from '../browser/userBrowserConnection';
import { createUserBrowserSessions, type UserBrowserWait } from '../browser/userBrowserSession';
import { createHtmlPreviewPdfs } from '../render/htmlPreview';

type LoggerLike = { error?: (name: string, payload?: unknown) => void };

export function createDesktopBrowsers(params: {
  /** The backend's own port, which no page may reach under any host name. */
  getBackendPort: () => number;
  /** Where the user's browsers keep their profiles (`app.getPath('appData')`). */
  applicationSupportDir: string;
  userDataDir: string;
  localArtifactRoot: string;
  postAnswer: (body: BrowserOperationAnswer) => Promise<unknown>;
  /** Shows the wait in the Action's chat; the returned closer takes it down. */
  showUserBrowserWait: (wait: UserBrowserWait) => () => void;
  /** Shows in the Action's live state that it has a live app page, until the closer runs. */
  beginAppBrowserPage: (actionId: string) => () => void;
  logger: LoggerLike | null;
}) {
  const pageViewer = createAppPageViewer({
    beginLivePage: params.beginAppBrowserPage,
    logger: params.logger,
  });
  const sessions = createBrowserSessions({
    getBackendPort: params.getBackendPort,
    pages: pageViewer.pageEvents,
    logger: params.logger,
  });
  const userSessions = createUserBrowserSessions({
    connect: (browser, waitForUser, signal) =>
      connectUserBrowser({
        browser,
        applicationSupportDir: params.applicationSupportDir,
        runner: runCommand,
        waitForUser,
        signal,
      }),
    showWait: params.showUserBrowserWait,
    getBackendPort: params.getBackendPort,
    logger: params.logger,
  });
  return {
    pageViewer,
    htmlPreviewPdfs: createHtmlPreviewPdfs({ userDataDir: params.userDataDir, sessions }),
    respond: createBrowserResponder({
      sessions,
      userSessions,
      localArtifactRoot: params.localArtifactRoot,
      postAnswer: params.postAnswer,
      logger: params.logger,
    }),
    close(sessionId: string): void {
      sessions.close(sessionId);
      userSessions.close(sessionId);
    },
    closeAll(): void {
      sessions.closeAll();
      userSessions.closeAll();
    },
  };
}
