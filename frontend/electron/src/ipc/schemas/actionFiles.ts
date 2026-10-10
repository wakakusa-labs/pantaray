/**
 * Zod schemas for the `actionFile:*` IPC payloads.
 */

import path from 'node:path';
import { z } from 'zod';

import { MAX_ID_LENGTH, MAX_PATH_LENGTH } from './limits';

const AbsolutePathSchema = z
  .string()
  .max(MAX_PATH_LENGTH)
  .refine((value) => path.isAbsolute(value), 'path must be absolute');

export const ActionFileOpenInputSchema = z.object({ path: AbsolutePathSchema }).strict();

export type ActionFileOpenInput = z.infer<typeof ActionFileOpenInputSchema>;

/** A file one Action names; main checks the Action's conversation before touching it. */
export const ActionFileRequestInputSchema = z
  .object({
    actionId: z.string().min(1).max(MAX_ID_LENGTH),
    path: AbsolutePathSchema,
  })
  .strict();
