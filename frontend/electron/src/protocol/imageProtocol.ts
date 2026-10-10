/**
 * `pantaray-image://local/{storage_path}` — the renderer's only way to display a stored image.
 *
 * The renderer holds logical `storage_path` values and nothing else: no absolute path, no bytes.
 * Every request is re-validated here, fail-closed, in this order (any failure is a bare 404 so a
 * probe cannot tell "not yours" from "not there"):
 *
 *   1. the URL host is the fixed `local`;
 *   2. the path decodes once, cleanly;
 *   3. the first segment is the currently signed-in user id;
 *   4. the whole path satisfies the shared `storage_path` rules;
 *   5. the resolved real path is inside `{artifact root}/generated/images` (symlink containment);
 *   6. the leading bytes are an allowed image, and agree with the extension.
 *
 * `Content-Type` comes from the sniffed bytes, never from the requested extension.
 */

import fs from 'fs';
import path from 'path';

import {
  ACTION_IMAGE_SCHEME,
  ACTION_IMAGE_URL_HOST,
  GENERATED_IMAGES_DIRECTORY_SEGMENTS,
  imageMimeTypeForStoragePath,
  isValidImageStoragePath,
  sniffImageMimeType,
  type ActionImageMimeType,
} from './imageStoragePath';

export type StoredImage = Readonly<{
  absolutePath: string;
  bytes: Buffer;
  mimeType: ActionImageMimeType;
}>;

export type StoredImageDeps = Readonly<{
  localArtifactRoot: string;
  getCurrentSubjectId: () => string | null;
}>;

/** Directory every user-scoped image lives under. Nothing outside it is ever served. */
export function generatedImagesDirectory(localArtifactRoot: string): string {
  return path.join(localArtifactRoot, ...GENERATED_IMAGES_DIRECTORY_SEGMENTS);
}

export function storedImageAbsolutePath(localArtifactRoot: string, storagePath: string): string {
  return path.join(generatedImagesDirectory(localArtifactRoot), ...storagePath.split('/'));
}

/**
 * Resolve a renderer-supplied `storage_path` to verified bytes, or null when anything is off.
 *
 * Shared by the protocol handler and by `actionImage:reveal` so both apply exactly one rule set.
 */
export function readVerifiedStoredImage(
  deps: StoredImageDeps,
  storagePath: string
): StoredImage | null {
  const userId = deps.getCurrentSubjectId();
  if (userId === null) return null;
  if (!isValidImageStoragePath({ userId, storagePath })) return null;

  const declaredMimeType = imageMimeTypeForStoragePath(storagePath);
  if (declaredMimeType === null) return null;

  let absolutePath: string;
  let root: string;
  try {
    // realpath both sides: a symlink planted inside the image directory must not reach out of it.
    root = fs.realpathSync(generatedImagesDirectory(deps.localArtifactRoot));
    absolutePath = fs.realpathSync(storedImageAbsolutePath(deps.localArtifactRoot, storagePath));
  } catch {
    return null;
  }
  if (absolutePath !== root && !absolutePath.startsWith(root + path.sep)) return null;

  let bytes: Buffer;
  try {
    const stats = fs.statSync(absolutePath);
    if (!stats.isFile()) return null;
    bytes = fs.readFileSync(absolutePath);
  } catch {
    return null;
  }

  const mimeType = sniffImageMimeType(bytes);
  if (mimeType === null || mimeType !== declaredMimeType) return null;
  return { absolutePath, bytes, mimeType };
}

/** `storage_path` carried by a `pantaray-image://` URL, or null when the URL is not one of ours. */
export function parseActionImageUrl(requestUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(requestUrl);
  } catch {
    return null;
  }
  if (url.protocol !== `${ACTION_IMAGE_SCHEME}:` || url.host !== ACTION_IMAGE_URL_HOST) return null;
  try {
    // Exactly one decode: a second pass would turn `%252e%252e` into `..`.
    return decodeURIComponent(url.pathname).replace(/^\//, '');
  } catch {
    return null;
  }
}

export function createActionImageProtocolHandler(
  deps: StoredImageDeps
): (request: { url: string }) => Response {
  return (request) => {
    const storagePath = parseActionImageUrl(request.url);
    const image = storagePath === null ? null : readVerifiedStoredImage(deps, storagePath);
    if (image === null) return new Response(null, { status: 404 });
    return new Response(new Uint8Array(image.bytes), {
      status: 200,
      headers: {
        'Content-Type': image.mimeType,
        'Content-Length': String(image.bytes.byteLength),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        'Content-Disposition': 'inline',
        'Content-Security-Policy': "default-src 'none'; sandbox",
      },
    });
  };
}

type ProtocolHandlerRegistrar = {
  handle: (scheme: string, handler: (request: { url: string }) => Response) => void;
};

/** Declared with every other privileged scheme in one call before `app.whenReady()`. */
export const ACTION_IMAGE_PRIVILEGED_SCHEME = {
  scheme: ACTION_IMAGE_SCHEME,
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: false,
    corsEnabled: false,
    stream: false,
    // Images stay subject to the window CSP, which names the scheme explicitly.
    bypassCSP: false,
  },
};

/** Must run after `app.whenReady()`. */
export function registerActionImageProtocol(
  registrar: ProtocolHandlerRegistrar,
  deps: StoredImageDeps
): void {
  registrar.handle(ACTION_IMAGE_SCHEME, createActionImageProtocolHandler(deps));
}
