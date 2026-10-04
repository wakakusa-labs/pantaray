/**
 * `action:attachFile` / `action:discardAttachment` payload contracts.
 *
 * Shared by Electron main (validation) and the renderer composer (routing and pre-flight
 * limits), so a file the Action cannot read is refused before its bytes cross the IPC boundary.
 * The local backend re-validates the name and size when the message is submitted.
 */

import { z } from 'zod';

/** Formats the Action's `read` tool opens (`local_runtime/tooling/documents`). */
export const ACTION_DOCUMENT_EXTENSIONS = ['.pdf', '.docx', '.xlsx', '.pptx', '.ipynb'] as const;
export type ActionDocumentExtension = (typeof ACTION_DOCUMENT_EXTENSIONS)[number];

/** `MAX_DOCUMENT_BYTES` (`local_runtime/tooling/documents/document_model.py`). */
export const ACTION_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024;

/** Longest file name macOS accepts for one path component, in UTF-8 bytes. */
const ATTACHMENT_NAME_MAX_UTF8_BYTES = 255;

// Path separators, the macOS display separator, NUL and every other C0/C1 control character.
// eslint-disable-next-line no-control-regex
const UNSAFE_NAME_CHARACTERS = /[/\\:\u0000-\u001f\u007f-\u009f]/g;

/** Lowercased allowed extension of a file name, or null. A leading-dot name has no stem. */
export function actionDocumentExtension(name: string): ActionDocumentExtension | null {
  const extensionIndex = name.lastIndexOf('.');
  if (extensionIndex <= 0) return null;
  const extension = name.slice(extensionIndex).toLowerCase();
  return (ACTION_DOCUMENT_EXTENSIONS as readonly string[]).includes(extension)
    ? (extension as ActionDocumentExtension)
    : null;
}

/**
 * The picker's name made safe to become one path component: NFC, unsafe characters replaced,
 * and the stem shortened (whole code points) until the name fits in 255 UTF-8 bytes. Called
 * only on a name with an allowed extension, which is kept so the name still says what it is.
 */
function sanitizeAttachmentName(name: string): string {
  const safe = name.normalize('NFC').replace(UNSAFE_NAME_CHARACTERS, '_');
  const extensionIndex = safe.lastIndexOf('.');
  const extension = safe.slice(extensionIndex);
  const stem = Array.from(safe.slice(0, extensionIndex));
  const utf8 = new TextEncoder();
  while (utf8.encode(stem.join('') + extension).length > ATTACHMENT_NAME_MAX_UTF8_BYTES) {
    stem.pop();
  }
  return stem.join('') + extension;
}

export const ActionAttachFileInputSchema = z
  .object({
    bytes: z
      .instanceof(ArrayBuffer)
      .refine((value) => value.byteLength > 0, 'file is empty')
      .refine((value) => value.byteLength <= ACTION_DOCUMENT_MAX_BYTES, 'file is too large'),
    // The allowed-extension rule also refuses an empty name, `.` and `..`.
    name: z.string().transform((value, context) => {
      const extension = actionDocumentExtension(value);
      if (extension === null) {
        context.addIssue({ code: z.ZodIssueCode.custom, message: 'unsupported file type' });
        return z.NEVER;
      }
      return { name: sanitizeAttachmentName(value), extension };
    }),
  })
  .strict();

export const ActionDiscardAttachmentInputSchema = z
  .object({ attachmentId: z.string().uuid() })
  .strict();

export type ActionAttachFileResult = Readonly<{
  attachmentId: string;
  name: string;
  byteSize: number;
}>;
