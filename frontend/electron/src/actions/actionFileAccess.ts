/**
 * Files an Action names, read or opened for the main window's preview.
 *
 * The renderer asks for a file by Action and absolute path. Main serves it only when one of
 * that Action's final answers links the path with `pantaray-file:///`. A compromised renderer
 * therefore reaches only what an Action of the signed-in user already put in front of them,
 * never an arbitrary file.
 */

import fs from 'fs';
import path from 'path';

import type { ActionConversationPageRequest } from './actionFetch';
import type { ActionConversationPage } from './actionContracts';
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

export type ActionFileRequest = Readonly<{ actionId: string; path: string }>;

export type ActionFileReadResult =
  | Readonly<{ kind: 'text'; text: string; truncated: boolean }>
  | Readonly<{ kind: 'image'; bytes: Uint8Array<ArrayBuffer>; mime: ActionImageMimeType }>
  /** A word-processor document converted to a web page by macOS `textutil`. */
  | Readonly<{ kind: 'html'; html: string }>
  | Readonly<{
      kind: 'unavailable';
      reason: 'not_found' | 'binary' | 'too_large' | 'conversion_failed';
    }>;

export type ActionFileOpenResult =
  | Readonly<{ kind: 'opened' }>
  | Readonly<{ kind: 'unavailable'; reason: 'not_found' | 'executable' | 'open_failed' }>;

export type ReadActionConversationPage = (
  request: ActionConversationPageRequest
) => Promise<ActionConversationPage>;

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

/** Reads the real path `resolveActionFile` returned. */
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

/** Opens the real path `resolveActionFile` returned in its default app. */
export async function openActionFileInApp(
  realPath: string,
  openPath: (realPath: string) => Promise<string>
): Promise<ActionFileOpenResult> {
  let stats: fs.Stats;
  try {
    stats = fs.statSync(realPath);
  } catch {
    return { kind: 'unavailable', reason: 'not_found' };
  }
  // A FIFO, socket or device is no document, and handing one to another app could stall it.
  if (!stats.isFile() && !stats.isDirectory()) return { kind: 'unavailable', reason: 'not_found' };
  if (isExecutable(realPath, stats)) return { kind: 'unavailable', reason: 'executable' };
  // shell.openPath resolves to an error message, empty on success.
  const failure = await openPath(realPath);
  return failure === '' ? { kind: 'opened' } : { kind: 'unavailable', reason: 'open_failed' };
}
