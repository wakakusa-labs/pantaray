/**
 * Capturing a browser session's page: a PNG of the viewport or the whole page through CDP, and a
 * PDF of the printed page. Each is handed to the runtime as a file under
 * `{artifact_root}/.tmp/browser/{operation_request_id}.{png|pdf}`, which it re-reads and checks
 * against the answer's size and digest.
 */

import type { Debugger, WebContents } from 'electron';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { writeFileAtomic } from '../atomicFile';
import type { BrowserArtifactClaim } from './browserContract';
import { BrowserOperationError } from './browserSession';
import { PAGE_VIEWPORT } from './pageWindowOptions';

/** An image or PDF captured from the page, not yet written. */
export type CapturedArtifact =
  | { kind: 'png'; bytes: Buffer; widthPx: number; heightPx: number }
  | { kind: 'pdf'; bytes: Buffer };

/**
 * Design limit: a full page is cut at this many CSS pixels (1280×8000 is 10 M pixels, well under
 * the 64 M decoded pixels a model request takes). Raise it when `page_too_tall` is reported for
 * pages whose lower part the agent needed.
 */
export const MAX_FULL_PAGE_HEIGHT_PX = 8_000;
const MAX_PDF_BYTES = 100 * 1024 * 1024;
/** The id becomes a file name, so it may not carry a separator or a dot segment. */
const OPERATION_REQUEST_ID = /^[A-Za-z0-9_-]{1,128}$/;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function decodePng(data: string): CapturedArtifact {
  const bytes = Buffer.from(data, 'base64');
  if (bytes.length < 24 || !bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) {
    throw new BrowserOperationError('BROWSER_ACTION_FAILED', 'The page capture was not a PNG.');
  }
  // IHDR is the first chunk: width and height are big-endian at bytes 16 and 20.
  return { kind: 'png', bytes, widthPx: bytes.readUInt32BE(16), heightPx: bytes.readUInt32BE(20) };
}

export async function captureViewport(cdp: Debugger): Promise<CapturedArtifact> {
  const { data } = (await cdp.sendCommand('Page.captureScreenshot', { format: 'png' })) as {
    data: string;
  };
  return decodePng(data);
}

/** The whole page at the viewport's width, cut at `MAX_FULL_PAGE_HEIGHT_PX`. */
export async function captureFullPage(
  cdp: Debugger
): Promise<{ artifact: CapturedArtifact; cut: boolean }> {
  const { cssContentSize } = (await cdp.sendCommand('Page.getLayoutMetrics')) as {
    cssContentSize: { height: number };
  };
  const contentHeight = Math.max(Math.ceil(cssContentSize.height), 1);
  const height = Math.min(contentHeight, MAX_FULL_PAGE_HEIGHT_PX);
  const { data } = (await cdp.sendCommand('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
    clip: { x: 0, y: 0, width: PAGE_VIEWPORT.width, height, scale: 1 },
  })) as { data: string };
  return { artifact: decodePng(data), cut: contentHeight > height };
}

/** Prints as the HTML print path always has: A4 unless the page sets its size, with backgrounds. */
export async function printPage(contents: WebContents): Promise<CapturedArtifact> {
  const bytes = await contents.printToPDF({
    printBackground: true,
    preferCSSPageSize: true,
    pageSize: 'A4',
  });
  if (bytes.byteLength > MAX_PDF_BYTES) {
    throw new BrowserOperationError(
      'BROWSER_ACTION_FAILED',
      'The printed PDF is larger than 100 MB.'
    );
  }
  return { kind: 'pdf', bytes };
}

/**
 * Removes every artifact handed over and not taken. Electron is the only writer there, so this is
 * for when no runtime can read one any more: its backend generation ended, maybe before the
 * answer reached it, in which case nothing else would ever remove the file.
 */
export function clearBrowserArtifacts(localArtifactRoot: string): void {
  fs.rmSync(path.join(localArtifactRoot, '.tmp', 'browser'), { recursive: true, force: true });
}

/** Where the artifact goes; the runtime derives the same path from the same id. */
export function browserArtifactPath(
  localArtifactRoot: string,
  operationRequestId: string,
  kind: CapturedArtifact['kind']
): string {
  if (!OPERATION_REQUEST_ID.test(operationRequestId)) {
    throw new Error('Refusing a browser operation request id that cannot name a file.');
  }
  return path.join(localArtifactRoot, '.tmp', 'browser', `${operationRequestId}.${kind}`);
}

/** Writes the artifact where the runtime will read it and returns the claim to send with it. */
export function writeArtifact(filePath: string, artifact: CapturedArtifact): BrowserArtifactClaim {
  writeFileAtomic(filePath, path.dirname(filePath), artifact.bytes);
  const claim = {
    byte_size: artifact.bytes.byteLength,
    sha256: createHash('sha256').update(artifact.bytes).digest('hex'),
  };
  return artifact.kind === 'png'
    ? { kind: 'png', ...claim, width_px: artifact.widthPx, height_px: artifact.heightPx }
    : { kind: 'pdf', ...claim };
}
