/**
 * An HTML file in the task preview, printed to PDF in a browser session of its own, so a chart
 * its script draws shows. The session opens for the one print and is closed after it, whatever
 * happened. The preview's frame then loads the PDF from `pantaray-action-file`
 * (actionFileProtocol), which checks the file's owner again and serves these bytes from memory:
 * no PDF is written to disk.
 *
 * The page loads files from its own folder only and nothing from the network, as the preview
 * never has, and nothing in private app storage outside its folder.
 */

import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { navigate } from '../browser/browserResponder';
import {
  BROWSER_NAVIGATION_BUDGET_MS,
  type BrowserFailureCode,
  type BrowserSessions,
} from '../browser/browserSession';
import { printPage } from '../browser/capture';
import {
  isWithinRoot,
  sameDirectoryOnDisk,
  type FileAccessScope,
  type SameDirectory,
} from '../browser/pagePolicy';

/**
 * Long enough for the frame to load the PDF, and to load it again (a reload, the viewer asking
 * twice) without printing again; short enough not to hold every PDF the pane showed. Opening
 * the file again prints it anew.
 */
const RENDERED_PDF_KEPT_MS = 60_000;

export type HtmlPreviewRender =
  | Readonly<{ kind: 'rendered'; renderId: string }>
  | Readonly<{ kind: 'failed'; code: BrowserFailureCode }>;

export type HtmlPreviewPdfs = Readonly<{
  /** Prints the real path `resolveTaskFile` returned. */
  render: (realPath: string) => Promise<HtmlPreviewRender>;
  /** The PDF printed from `realPath` under `renderId` while it is kept, or null. */
  readRendered: (renderId: string, realPath: string) => Buffer | null;
}>;

/**
 * Pantaray's private app storage as the runtime's `app_owned_roots` names it: the app data
 * folder, which holds the database and the artifact root, and the command temp parent.
 */
export function appPrivateRoots(userDataDir: string): string[] {
  const home = fs.realpathSync.native(os.homedir());
  return [
    fs.realpathSync.native(userDataDir),
    path.join(home, 'Library', 'Caches', 'pantaray-command-temp'),
  ];
}

/**
 * The page's own folder. A folder in private storage is the owner's own (an Action's workspace,
 * a Suggestion's folder: the only places either writes there), so it stays loadable there, as
 * the runtime lets an Action read its workspace. Private storage below any other folder does not.
 */
export function previewFileScope(
  htmlPath: string,
  privateRoots: string[],
  sameDirectory: SameDirectory
): FileAccessScope {
  const folder = path.dirname(htmlPath);
  const inPrivateStorage = privateRoots.some((root) => isWithinRoot(folder, root, sameDirectory));
  return {
    fileRoots: [folder],
    privateRoots,
    privateRootExceptions: inPrivateStorage ? [folder] : [],
    // The user reads the preview, not an agent; files holding secrets are kept from agents.
    secretPaths: [],
  };
}

export function createHtmlPreviewPdfs(params: {
  userDataDir: string;
  sessions: BrowserSessions;
}): HtmlPreviewPdfs {
  const kept = new Map<string, Readonly<{ realPath: string; pdf: Buffer }>>();
  return {
    render: async (realPath) => {
      const scope = previewFileScope(
        realPath,
        appPrivateRoots(params.userDataDir),
        sameDirectoryOnDisk
      );
      const operation = { action: 'navigate', target: { kind: 'file', path: realPath } } as const;
      const sessionId = `html-preview-${randomUUID()}`;
      let settled;
      try {
        // Loading and printing share one budget, as long as a navigation may take.
        settled = await params.sessions.run(
          {
            session_id: sessionId,
            operation,
            policy: {
              allow_network: false,
              file_roots: scope.fileRoots,
              private_roots: scope.privateRoots,
              private_root_exceptions: scope.privateRootExceptions,
              secret_paths: scope.secretPaths,
              write_hosts: [],
            },
          },
          BROWSER_NAVIGATION_BUDGET_MS,
          async (page) => {
            await navigate(page.contents, operation.target);
            // A font is fetched only once text uses it, which can be after `load`.
            await page.contents.executeJavaScript('document.fonts.ready.then(() => undefined)');
            return (await printPage(page.contents)).bytes;
          }
        );
      } finally {
        params.sessions.close(sessionId);
      }
      if (settled.status === 'failed') {
        console.error('Failed to render an HTML file for preview', settled.code);
        return { kind: 'failed', code: settled.code };
      }
      const renderId = randomUUID();
      kept.set(renderId, { realPath, pdf: settled.value });
      setTimeout(() => kept.delete(renderId), RENDERED_PDF_KEPT_MS).unref();
      return { kind: 'rendered', renderId };
    },
    readRendered: (renderId, realPath) => {
      const entry = kept.get(renderId);
      return entry?.realPath === realPath ? entry.pdf : null;
    },
  };
}
