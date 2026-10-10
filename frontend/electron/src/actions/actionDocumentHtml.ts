/**
 * A word-processor document an Action names, as a web page for the preview.
 *
 * macOS ships `textutil`, which reads .docx, .doc, .rtf and .odt and writes HTML. It runs as a
 * child process with an argument vector (never a shell), a time limit and an output cap. The
 * HTML it writes is rendered like any other untrusted page: in an empty-sandbox frame.
 */

import fs from 'fs';
import path from 'path';

import type { ActionFileReadResult } from './actionFileAccess';

const TEXTUTIL = '/usr/bin/textutil';
/** textutil's `-format` name for each extension it converts here. */
const TEXTUTIL_FORMAT_BY_EXTENSION: Readonly<Record<string, string>> = {
  '.docx': 'docx',
  '.doc': 'doc',
  '.rtf': 'rtf',
  '.odt': 'odt',
};
/** The same bound the agents' document reader puts on one document. */
export const DOCUMENT_HTML_MAX_INPUT_BYTES = 20 * 1024 * 1024;
export const DOCUMENT_HTML_MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
export const DOCUMENT_HTML_TIMEOUT_MS = 15_000;

/** `execFile`'s stdout, as text; injected so the call can be checked without running it. */
export type RunFile = (
  file: string,
  args: readonly string[],
  options: Readonly<{ timeout: number; maxBuffer: number }>
) => Promise<string>;

/** textutil's input format for a word-processor document, or null for any other file. */
export function documentHtmlFormat(filePath: string): string | null {
  return TEXTUTIL_FORMAT_BY_EXTENSION[path.extname(filePath).toLowerCase()] ?? null;
}

/** Converts the real path `resolveActionFile` returned, read as `format`. */
export async function readDocumentAsHtml(
  filePath: string,
  format: string,
  runFile: RunFile
): Promise<ActionFileReadResult> {
  let stats: fs.Stats;
  try {
    stats = fs.statSync(filePath);
  } catch {
    return { kind: 'unavailable', reason: 'not_found' };
  }
  if (!stats.isFile()) return { kind: 'unavailable', reason: 'not_found' };
  if (stats.size > DOCUMENT_HTML_MAX_INPUT_BYTES) {
    return { kind: 'unavailable', reason: 'too_large' };
  }
  // -format stops textutil from sniffing the bytes: a file that is really HTML would otherwise
  // be read as HTML and fetch its remote resources before any CSP applies. -noload refuses
  // sub-resources outright, and -nostore keeps it from writing them beside the output.
  const args = [
    '-format',
    format,
    '-convert',
    'html',
    '-encoding',
    'UTF-8',
    '-noload',
    '-nostore',
    '-stdout',
    filePath,
  ];
  try {
    const html = await runFile(TEXTUTIL, args, {
      timeout: DOCUMENT_HTML_TIMEOUT_MS,
      maxBuffer: DOCUMENT_HTML_MAX_OUTPUT_BYTES,
    });
    // textutil exits 0 with nothing on stdout for a file it cannot read.
    if (html.trim() === '') return { kind: 'unavailable', reason: 'conversion_failed' };
    return { kind: 'html', html };
  } catch (error) {
    if ((error as { code?: unknown }).code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
      return { kind: 'unavailable', reason: 'too_large' };
    }
    console.error('textutil could not convert a document for preview', error);
    return { kind: 'unavailable', reason: 'conversion_failed' };
  }
}
