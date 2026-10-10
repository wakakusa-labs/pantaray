/**
 * Files an Action names, read or opened for the main window's preview.
 *
 * The renderer asks for a file by Action and absolute path. Main serves it only when that
 * Action's own conversation names the path: a `pantaray-file:///` link in a final answer, or
 * the target of an `apply_patch` step. A compromised renderer therefore reaches only what an
 * Action of the signed-in user already put in front of them, never an arbitrary file.
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
  | Readonly<{ kind: 'unavailable'; reason: 'not_found' | 'binary' | 'too_large' }>;

export type ActionFileOpenResult =
  | Readonly<{ kind: 'opened' }>
  | Readonly<{ kind: 'unavailable'; reason: 'not_found' | 'executable' | 'open_failed' }>;

export type ReadActionConversationPage = (
  request: ActionConversationPageRequest
) => Promise<ActionConversationPage>;

/**
 * The absolute paths one conversation page names. A patch's subject is the path its call gave,
 * cut at 120 characters with an ellipsis, so only an absolute, uncut one names a file.
 */
export function actionNamedPaths(page: ActionConversationPage): string[] {
  const paths: string[] = [];
  for (const run of page.runs) {
    if (run.final_output !== null) paths.push(...pantarayFilePaths(run.final_output));
    for (const entry of run.entries) {
      if (
        entry.step_kind === 'tool' &&
        entry.label === 'apply_patch' &&
        entry.subject !== null &&
        path.isAbsolute(entry.subject) &&
        !entry.subject.endsWith('…')
      ) {
        paths.push(entry.subject);
      }
    }
  }
  return paths;
}

/** Reads the Action's pages, newest first, until one names the path. */
export async function isActionFile(
  readPage: ReadActionConversationPage,
  request: ActionFileRequest
): Promise<boolean> {
  const wanted = path.normalize(request.path);
  let cursor: string | null = null;
  do {
    const page = await readPage({
      actionId: request.actionId,
      cursor,
      limit: ACTION_CONVERSATION_MAX_PAGE_SIZE,
    });
    if (actionNamedPaths(page).some((named) => path.normalize(named) === wanted)) return true;
    cursor = page.next_cursor;
  } while (cursor !== null);
  return false;
}

/** The open regular file, or null when the path names nothing readable. */
export function openRegularFile(filePath: string): { fd: number; size: number } | null {
  let fd: number;
  try {
    fd = fs.openSync(filePath, fs.constants.O_RDONLY);
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

/** Reads a file the caller already checked with `isActionFile`. */
export function readActionFile(filePath: string): ActionFileReadResult {
  const file = openRegularFile(filePath);
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

function isExecutable(requestedPath: string, realPath: string, stats: fs.Stats): boolean {
  const named = [requestedPath, realPath].map((candidate) => path.extname(candidate).toLowerCase());
  if (named.some((extension) => EXECUTABLE_EXTENSIONS.has(extension))) return true;
  if (stats.isDirectory()) {
    return fs.existsSync(path.join(realPath, 'Contents', 'Info.plist'));
  }
  return (stats.mode & EXECUTABLE_MODE_BITS) !== 0;
}

/** Opens a file the caller already checked with `isActionFile` in its default app. */
export async function openActionFileInApp(
  filePath: string,
  openPath: (realPath: string) => Promise<string>
): Promise<ActionFileOpenResult> {
  let realPath: string;
  let stats: fs.Stats;
  try {
    realPath = fs.realpathSync(filePath);
    stats = fs.statSync(realPath);
  } catch {
    return { kind: 'unavailable', reason: 'not_found' };
  }
  if (isExecutable(filePath, realPath, stats)) return { kind: 'unavailable', reason: 'executable' };
  // shell.openPath resolves to an error message, empty on success.
  const failure = await openPath(realPath);
  return failure === '' ? { kind: 'opened' } : { kind: 'unavailable', reason: 'open_failed' };
}
