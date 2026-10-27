/**
 * Files an Action names or a Suggestion made, read or opened for the main window's preview.
 *
 * The renderer asks for a file by its owner and path. Main serves an Action's file only when one
 * of that Action's final answers links the absolute path with `pantaray-file:///`, and a
 * Suggestion's file only when the backend's stored list for that Suggestion holds the relative
 * path and it resolves inside the Suggestion's folder. A compromised renderer therefore reaches
 * only what the signed-in user's own work already put in front of them, never an arbitrary file.
 */

import fs from 'fs';
import path from 'path';

import type { RunFile } from './actionDocumentHtml';
import type { ActionConversationPageRequest } from './actionFetch';
import type { ActionConversationPage } from './actionContracts';
import type { OverlayBootstrapResponse } from '../orchestration/contracts';
import { pantarayFilePaths } from './pantarayFileLinks';
import { sniffImageMimeType, type ActionImageMimeType } from '../protocol/imageStoragePath';
import { ACTION_CONVERSATION_MAX_PAGE_SIZE } from '../ipc/schemas/actions';

/** Keeps the preview responsive; the rest of a long file is one click away in its app. */
export const ACTION_FILE_TEXT_MAX_BYTES = 512 * 1024;
/** One IPC payload; a larger picture opens in its app instead. */
export const ACTION_FILE_IMAGE_MAX_BYTES = 16 * 1024 * 1024;
const IMAGE_SNIFF_BYTES = 12;

/*
 * `shell.openPath` runs these instead of showing them: apps, shell and Terminal scripts, Java
 * archives, and Finder locations that open their target. Executable files and bundle
 * directories are refused by what they are, whatever their name.
 */
const EXECUTABLE_EXTENSIONS = new Set([
  '.app',
  '.command',
  '.sh',
  '.tool',
  '.terminal',
  '.jar',
  '.fileloc',
]);
const EXECUTABLE_MODE_BITS = 0o111;

const OPEN_COMMAND = '/usr/bin/open';
/** `open` returns once Launch Services has the request; it never waits for the app. */
const OPEN_WITH_LIMITS = { timeout: 30_000, maxBuffer: 64 * 1024 } as const;

export type ActionFileRequest = Readonly<{ actionId: string; path: string }>;

/** Whose file the preview shows: an Action's, by absolute path, or a Suggestion's, by relative path. */
export type TaskFileTarget = Readonly<{ kind: 'action' | 'suggestion'; id: string }>;
export type TaskFileRequest = Readonly<{ target: TaskFileTarget; path: string }>;

export type ActionFileReadResult =
  | Readonly<{ kind: 'text'; text: string; truncated: boolean }>
  | Readonly<{ kind: 'image'; bytes: Uint8Array<ArrayBuffer>; mime: ActionImageMimeType }>
  /** A word-processor document converted to a web page by macOS `textutil`. */
  | Readonly<{ kind: 'html'; html: string }>
  /** An HTML file printed to PDF, at its `pantaray-action-file` URL (`htmlPreview`). */
  | Readonly<{ kind: 'pdf'; url: string }>
  | Readonly<{
      kind: 'unavailable';
      reason: 'not_found' | 'binary' | 'too_large' | 'conversion_failed' | 'render_failed';
    }>;

type ActionFileUnavailable = Readonly<{
  kind: 'unavailable';
  reason: 'not_found' | 'executable' | 'open_failed';
}>;

export type ActionFileOpenResult = Readonly<{ kind: 'opened' }> | ActionFileUnavailable;

export type ActionFileOpenWithResult =
  | ActionFileOpenResult
  | Readonly<{ kind: 'cancelled' }>
  | Readonly<{ kind: 'unavailable'; reason: 'not_an_app' }>;

export type ReadActionConversationPage = (
  request: ActionConversationPageRequest
) => Promise<ActionConversationPage>;

/** The backend's record of a Suggestion, owned by the signed-in user; null for no id. */
export type ReadSuggestionBootstrap = (
  suggestionId: string
) => Promise<OverlayBootstrapResponse | null>;

export type TaskFileSources = Readonly<{
  readConversationPage: ReadActionConversationPage;
  readSuggestionBootstrap: ReadSuggestionBootstrap;
  /** The signed-in local owner, as `suggestion:read` checks it; null when signed out. */
  getCurrentSubjectId: () => string | null;
}>;

/**
 * The paths one conversation page names: the `pantaray-file:///` links in its final answers,
 * decoded exactly. A tool step's subject is display text (whitespace collapsed, cut at 120
 * characters), not a path, so it never authorizes a file.
 */
export function actionNamedPaths(page: ActionConversationPage): string[] {
  return page.runs.flatMap((run) =>
    run.final_output === null ? [] : pantarayFilePaths(run.final_output)
  );
}

// The OS realpath resolves `..` after the symlink before it, as open() does; Node's own
// realpathSync normalizes the string first and would resolve a different file.
function realpathOrNull(filePath: string): string | null {
  try {
    return fs.realpathSync.native(filePath);
  } catch {
    return null;
  }
}

/**
 * The real path of the requested file when the Action names that file, or null. Paths are
 * compared after every symlink is resolved, never as strings: `/work/link/../a.txt` normalizes
 * to `/work/a.txt` but opens whatever `link/..` reaches. Callers use only the path this returns.
 * Reads the Action's pages, newest first, until one names the file.
 */
export async function resolveActionFile(
  readPage: ReadActionConversationPage,
  request: ActionFileRequest
): Promise<string | null> {
  const wanted = realpathOrNull(request.path);
  if (wanted === null) return null;
  const resolved = new Map<string, string | null>();
  let cursor: string | null = null;
  do {
    const page = await readPage({
      actionId: request.actionId,
      cursor,
      limit: ACTION_CONVERSATION_MAX_PAGE_SIZE,
    });
    for (const named of actionNamedPaths(page)) {
      if (!resolved.has(named)) resolved.set(named, realpathOrNull(named));
      if (resolved.get(named) === wanted) return wanted;
    }
    cursor = page.next_cursor;
  } while (cursor !== null);
  return null;
}

/**
 * The real path of a file a Suggestion made, or null. The relative path must be one the backend
 * stored for that Suggestion, and its real path must stay inside the Suggestion's real folder, so
 * a symlink the make stage left cannot lead out of it.
 */
export async function resolveSuggestionFile(
  readBootstrap: ReadSuggestionBootstrap,
  suggestionId: string,
  relativePath: string
): Promise<string | null> {
  const bootstrap = await readBootstrap(suggestionId);
  if (bootstrap === null || !bootstrap.snapshot.artifacts.includes(relativePath)) return null;
  const root = realpathOrNull(bootstrap.artifactsRoot);
  const wanted = realpathOrNull(path.join(bootstrap.artifactsRoot, relativePath));
  if (root === null || wanted === null) return null;
  return wanted.startsWith(root + path.sep) ? wanted : null;
}

/**
 * The real path of the requested file when its owner names it, or null. The backend answered for
 * the owner signed in when the request arrived, so an answer that comes back after the owner
 * changed or signed out authorizes nothing.
 */
export async function resolveTaskFile(
  sources: TaskFileSources,
  request: TaskFileRequest
): Promise<string | null> {
  const subjectId = sources.getCurrentSubjectId();
  if (subjectId === null) return null;
  const { target } = request;
  const realPath =
    target.kind === 'action'
      ? await resolveActionFile(sources.readConversationPage, {
          actionId: target.id,
          path: request.path,
        })
      : await resolveSuggestionFile(sources.readSuggestionBootstrap, target.id, request.path);
  return sources.getCurrentSubjectId() === subjectId ? realPath : null;
}

/**
 * The open regular file at a resolved real path, or null. O_NONBLOCK keeps a FIFO with no
 * writer from blocking the main process in open(); fstat then turns it, a socket or a device
 * away before anything is read.
 */
export function openRegularFile(realPath: string): { fd: number; size: number } | null {
  let fd: number;
  try {
    // Threat model: a process of this user could swap a parent folder between realpath and open,
    // but it could read the file itself, so this window gives it no reach it lacks.
    fd = fs.openSync(
      realPath,
      fs.constants.O_RDONLY | fs.constants.O_NONBLOCK | fs.constants.O_NOFOLLOW
    );
  } catch {
    return null;
  }
  const stats = fs.fstatSync(fd);
  if (!stats.isFile()) {
    fs.closeSync(fd);
    return null;
  }
  return { fd, size: stats.size };
}

function readAt(fd: number, length: number): Buffer {
  const buffer = Buffer.alloc(length);
  let filled = 0;
  while (filled < length) {
    const read = fs.readSync(fd, buffer, filled, length - filled, filled);
    if (read === 0) break;
    filled += read;
  }
  return buffer.subarray(0, filled);
}

/** Reads the real path `resolveTaskFile` returned. */
export function readActionFile(realPath: string): ActionFileReadResult {
  const file = openRegularFile(realPath);
  if (file === null) return { kind: 'unavailable', reason: 'not_found' };
  try {
    // The bytes decide: an image by its signature, text by decoding as UTF-8.
    if (sniffImageMimeType(readAt(file.fd, IMAGE_SNIFF_BYTES)) !== null) {
      if (file.size > ACTION_FILE_IMAGE_MAX_BYTES) {
        return { kind: 'unavailable', reason: 'too_large' };
      }
      const bytes = readAt(file.fd, file.size);
      const mime = sniffImageMimeType(bytes);
      if (mime === null) return { kind: 'unavailable', reason: 'binary' };
      return { kind: 'image', bytes: new Uint8Array(bytes), mime };
    }
    const head = readAt(file.fd, ACTION_FILE_TEXT_MAX_BYTES + 1);
    const truncated = head.length > ACTION_FILE_TEXT_MAX_BYTES;
    try {
      // stream: a cut through a multi-byte character holds those bytes back instead of failing.
      const text = new TextDecoder('utf-8', { fatal: true }).decode(
        head.subarray(0, ACTION_FILE_TEXT_MAX_BYTES),
        { stream: truncated }
      );
      return { kind: 'text', text, truncated };
    } catch {
      return { kind: 'unavailable', reason: 'binary' };
    }
  } finally {
    fs.closeSync(file.fd);
  }
}

function isExecutable(realPath: string, stats: fs.Stats): boolean {
  if (EXECUTABLE_EXTENSIONS.has(path.extname(realPath).toLowerCase())) return true;
  if (stats.isDirectory()) {
    return fs.existsSync(path.join(realPath, 'Contents', 'Info.plist'));
  }
  return (stats.mode & EXECUTABLE_MODE_BITS) !== 0;
}

/** Why the real path `resolveTaskFile` returned must not go to an app, or null when it may. */
function refuseToOpen(realPath: string): ActionFileUnavailable | null {
  let stats: fs.Stats;
  try {
    stats = fs.statSync(realPath);
  } catch {
    return { kind: 'unavailable', reason: 'not_found' };
  }
  // A FIFO, socket or device is no document, and handing one to another app could stall it.
  if (!stats.isFile() && !stats.isDirectory()) return { kind: 'unavailable', reason: 'not_found' };
  if (isExecutable(realPath, stats)) return { kind: 'unavailable', reason: 'executable' };
  return null;
}

/** Opens the real path `resolveTaskFile` returned in its default app. */
export async function openActionFileInApp(
  realPath: string,
  openPath: (realPath: string) => Promise<string>
): Promise<ActionFileOpenResult> {
  const refusal = refuseToOpen(realPath);
  if (refusal !== null) return refusal;
  // shell.openPath resolves to an error message, empty on success.
  const failure = await openPath(realPath);
  return failure === '' ? { kind: 'opened' } : { kind: 'unavailable', reason: 'open_failed' };
}

function isAppBundle(appPath: string): boolean {
  if (path.extname(appPath).toLowerCase() !== '.app') return false;
  try {
    return (
      fs.statSync(appPath).isDirectory() &&
      fs.statSync(path.join(appPath, 'Contents', 'Info.plist')).isFile()
    );
  } catch {
    return false;
  }
}

/**
 * Opens the real path `resolveTaskFile` returned in an app the user picks. The file is
 * vetted before the user is asked, and the app must be an existing `.app` bundle: `open -a`
 * would otherwise search for an app by that name.
 */
export async function openActionFileWithApp(
  realPath: string,
  chooseApp: () => Promise<string | null>,
  runFile: RunFile
): Promise<ActionFileOpenWithResult> {
  const refusal = refuseToOpen(realPath);
  if (refusal !== null) return refusal;
  const appPath = await chooseApp();
  if (appPath === null) return { kind: 'cancelled' };
  if (!isAppBundle(appPath)) return { kind: 'unavailable', reason: 'not_an_app' };
  try {
    await runFile(OPEN_COMMAND, ['-a', appPath, realPath], OPEN_WITH_LIMITS);
    return { kind: 'opened' };
  } catch {
    // `open` exits non-zero when the app cannot open the file; the user sees the failure.
    return { kind: 'unavailable', reason: 'open_failed' };
  }
}
