/**
 * Zod schemas for the shared task drafts (`action:openDraft`, `action:updateDraft`,
 * `action:closeDraft`). A draft is what a task's composer holds before it is sent; the limits
 * here only bound what main keeps in memory, and the send checks what may go out.
 */

import { z } from 'zod';

import { ACTION_MESSAGE_MAX_FILES, CanonicalIdentitySchema } from '../../actions/actionContracts';
import { ACTION_IMAGE_MAX_PER_MESSAGE } from './actionImages';

// Design limit: drafts live in memory per task; these caps keep one renderer from growing it.
const TASK_DRAFT_MAX_TEXT_LENGTH = 256_000;
const TASK_DRAFT_MAX_MENTIONS = 256;
const TASK_DRAFT_MAX_PATHS = 32;
const TASK_DRAFT_MAX_REF_LENGTH = 1_024;

const WORK_KEY = /^(action|suggestion):(.+)$/s;

/** The task a draft belongs to, as History names it: `action:<id>` or `suggestion:<id>`. */
export const TaskDraftWorkSchema = z
  .string()
  .refine((value) => {
    const match = WORK_KEY.exec(value);
    return match !== null && match[2] === match[2].trim();
  }, 'work key must be action:<id> or suggestion:<id>')
  .transform((value) => value as `action:${string}` | `suggestion:${string}`);

const RefSchema = z.string().min(1).max(TASK_DRAFT_MAX_REF_LENGTH);

export const TaskDraftSchema = z
  .object({
    text: z.string().max(TASK_DRAFT_MAX_TEXT_LENGTH),
    // UTF-16 offsets into `text`, as the composer keeps them.
    mentions: z
      .array(
        z
          .object({
            projectId: CanonicalIdentitySchema,
            displayName: RefSchema,
            paths: z.array(RefSchema).max(TASK_DRAFT_MAX_PATHS),
            start: z.number().int().nonnegative(),
            end: z.number().int().nonnegative(),
          })
          .strict()
      )
      .max(TASK_DRAFT_MAX_MENTIONS),
    // References to what main already wrote or staged; never the bytes.
    attachments: z
      .array(
        z.discriminatedUnion('kind', [
          z.object({ kind: z.literal('image'), storagePath: RefSchema }).strict(),
          z
            .object({
              kind: z.literal('file'),
              attachmentId: z.string().uuid(),
              name: RefSchema,
              byteSize: z.number().int().positive(),
            })
            .strict(),
        ])
      )
      .max(ACTION_IMAGE_MAX_PER_MESSAGE + ACTION_MESSAGE_MAX_FILES),
  })
  .strict();

export type TaskDraftWork = z.infer<typeof TaskDraftWorkSchema>;
export type TaskDraft = z.infer<typeof TaskDraftSchema>;

export const TaskDraftOpenRequestSchema = z.object({ work: TaskDraftWorkSchema }).strict();

export const TaskDraftUpdateRequestSchema = z
  .object({ work: TaskDraftWorkSchema, draft: TaskDraftSchema })
  .strict();
