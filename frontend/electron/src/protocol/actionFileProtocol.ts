/**
 * `pantaray-action-file://local/?kind={action|suggestion}&id={id}&path={path}` — a PDF an Action
 * names or a Suggestion made, for Chromium's PDF viewer in the main window's preview frame.
 *
 * Every request passes the same check as `actionFile:read` (`resolveTaskFile`). Only a file whose
 * bytes start as a PDF is served, as `application/pdf`, so an HTML or script file can never be
 * loaded into a frame through this scheme. Any failure is a bare 404.
 */

import fs from 'fs';
import path from 'path';
import { Readable } from 'stream';

import {
  openRegularFile,
  resolveTaskFile,
  type TaskFileRequest,
  type TaskFileSources,
} from '../actions/actionFileAccess';
import { ACTION_FILE_SCHEME, ACTION_FILE_URL_HOST } from './actionFileUrl';

const PDF_SIGNATURE = Buffer.from('%PDF-');

export function parseActionFileUrl(requestUrl: string): TaskFileRequest | null {
  let url: URL;
  try {
    url = new URL(requestUrl);
  } catch {
    return null;
  }
  if (url.protocol !== `${ACTION_FILE_SCHEME}:` || url.host !== ACTION_FILE_URL_HOST) return null;
  const kind = url.searchParams.get('kind');
  const id = url.searchParams.get('id');
  const filePath = url.searchParams.get('path');
  if ((kind !== 'action' && kind !== 'suggestion') || !id || !filePath) return null;
  if (kind === 'action' && !path.isAbsolute(filePath)) return null;
  return { target: { kind, id }, path: filePath };
}

export function createActionFileProtocolHandler(
  deps: TaskFileSources
): (request: { url: string }) => Promise<Response> {
  const notFound = () => new Response(null, { status: 404 });
  return async (request) => {
    const fileRequest = parseActionFileUrl(request.url);
    if (fileRequest === null) return notFound();
    let realPath: string | null;
    try {
      realPath = await resolveTaskFile(deps, fileRequest);
    } catch (error) {
      console.error('Failed to read the owner of a file request', error);
      return notFound();
    }
    if (realPath === null) return notFound();
    const file = openRegularFile(realPath);
    if (file === null) return notFound();
    const head = Buffer.alloc(PDF_SIGNATURE.length);
    const headLength = fs.readSync(file.fd, head, 0, head.length, 0);
    if (headLength !== head.length || !head.equals(PDF_SIGNATURE)) {
      fs.closeSync(file.fd);
      return notFound();
    }
    const body = Readable.toWeb(fs.createReadStream('', { fd: file.fd, start: 0 }));
    return new Response(body as ReadableStream<Uint8Array>, {
      status: 200,
      headers: {
        'Content-Type': 'application/pdf',
        'Content-Length': String(file.size),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
      },
    });
  };
}

type ProtocolHandlerRegistrar = {
  handle: (scheme: string, handler: (request: { url: string }) => Promise<Response>) => void;
};

/** Declared with every other privileged scheme in one call before `app.whenReady()`. */
export const ACTION_FILE_PRIVILEGED_SCHEME = {
  scheme: ACTION_FILE_SCHEME,
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: false,
    corsEnabled: false,
    // The PDF viewer reads the body as a stream.
    stream: true,
    // Frames stay subject to the window CSP, which names the scheme in frame-src.
    bypassCSP: false,
  },
};

/** Must run after `app.whenReady()`. */
export function registerActionFileProtocol(
  registrar: ProtocolHandlerRegistrar,
  deps: TaskFileSources
): void {
  registrar.handle(ACTION_FILE_SCHEME, createActionFileProtocolHandler(deps));
}
