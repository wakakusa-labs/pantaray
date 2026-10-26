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

/**
 * A file an Action names (absolute path) or a Suggestion made (path relative to its folder); main
 * checks it against its owner before touching it.
 */
export const TaskFileRequestInputSchema = z
  .object({
    target: z
      .object({
        kind: z.enum(['action', 'suggestion']),
        id: z.string().min(1).max(MAX_ID_LENGTH),
      })
      .strict(),
    path: z.string().min(1).max(MAX_PATH_LENGTH),
  })
  .strict()
  .refine(
    (request) => request.target.kind === 'suggestion' || path.isAbsolute(request.path),
    'an Action file path must be absolute'
  );
