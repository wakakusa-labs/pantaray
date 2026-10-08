/**
 * Zod schema for the `clipboard:writeText` IPC payload.
 */

import { z } from 'zod';

import { MAX_CLIPBOARD_TEXT_LENGTH } from './limits';

export const ClipboardWriteTextInputSchema = z
  .object({ text: z.string().max(MAX_CLIPBOARD_TEXT_LENGTH) })
  .strict();

export type ClipboardWriteTextInput = z.infer<typeof ClipboardWriteTextInputSchema>;
