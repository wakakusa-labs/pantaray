/**
 * Zod schemas for the shared task drafts (`action:openDraft`, `action:updateDraft`,
 * `action:closeDraft`). A draft is what a task's composer holds before it is sent; the limits
 * here only bound what main keeps in memory, and the send checks what may go out.
 */

import { z } from 'zod';

import {
  ACTION_MESSAGE_MAX_FILES,
  ActionMessageRequestSchema,
  CanonicalIdentitySchema,
} from '../../actions/actionContracts';
import { ACTION_IMAGE_MAX_PER_MESSAGE } from './actionImages';

// Design limit: drafts live in memory per task; these caps keep one renderer from growing it.
const TASK_DRAFT_MAX_TEXT_LENGTH = 256_000;
const TASK_DRAFT_MAX_MENTIONS = 256;
const TASK_DRAFT_MAX_PATHS = 32;
const TASK_DRAFT_MAX_REF_LENGTH = 1_024;
const TASK_DRAFT_MAX_QUOTES = 64;

const WORK_KEY = /^(action|suggestion):(.+)$/s;

/**
 * The task a draft belongs to, as History names it: `action:<id>`, `suggestion:<id>`, or `new`
 * for the main window's new task before its first send opens an Action.
 */
export const TaskDraftWorkSchema = z
  .string()
  .refine((value) => {
    if (value === 'new') return true;
    const match = WORK_KEY.exec(value);
    return match !== null && match[2] === match[2].trim();
  }, 'work key must be action:<id>, suggestion:<id> or new')
  .transform((value) => value as `action:${string}` | `suggestion:${string}` | 'new');

const RefSchema = z.string().min(1).max(TASK_DRAFT_MAX_REF_LENGTH);
const OffsetSchema = z.number().int().nonnegative();

export const TaskDraftSchema = z
  .object({
    text: z.string().max(TASK_DRAFT_MAX_TEXT_LENGTH),
    // UTF-16 offsets into `text`, as the composer keeps them: projects, and tasks and suggestions.
    mentions: z
      .array(
        z.discriminatedUnion('kind', [
          z
            .object({
              kind: z.literal('project'),
              projectId: CanonicalIdentitySchema,
              displayName: RefSchema,
              paths: z.array(RefSchema).max(TASK_DRAFT_MAX_PATHS),
              start: OffsetSchema,
              end: OffsetSchema,
            })
            .strict(),
          z
            .object({
              kind: z.enum(['action', 'suggestion']),
              id: CanonicalIdentitySchema,
              displayName: RefSchema,
              start: OffsetSchema,
              end: OffsetSchema,
            })
            .strict(),
        ])
      )
      .max(TASK_DRAFT_MAX_MENTIONS),
    // Passages quoted from the conversation with the user's comment, in the order added.
    quotes: z
      .array(
        z
          .object({
            id: z.string().uuid(),
            text: z.string().min(1).max(TASK_DRAFT_MAX_TEXT_LENGTH),
            comment: z.string().max(TASK_DRAFT_MAX_TEXT_LENGTH),
          })
          .strict()
      )
      .max(TASK_DRAFT_MAX_QUOTES),
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
    // A send whose outcome is unknown. The backend dedupes on its message_id, so it is retried
    // as this exact request, never as a new message that could run a second time.
    retry: ActionMessageRequestSchema.nullable(),
  })
  .strict();

export type TaskDraftWork = z.infer<typeof TaskDraftWorkSchema>;
export type TaskDraft = z.infer<typeof TaskDraftSchema>;

export const TaskDraftOpenRequestSchema = z.object({ work: TaskDraftWorkSchema }).strict();

export const TaskDraftUpdateRequestSchema = z
  .object({ work: TaskDraftWorkSchema, draft: TaskDraftSchema })
  .strict();
