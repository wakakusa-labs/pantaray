/**
 * The user's single chat as Electron main reads it from the local backend
 * (`pantaray_agents/schema/chat.py`) and hands it to the main window.
 *
 * A card names an Action or a Suggestion by id only; its status is read from that Action or
 * Suggestion when it is drawn, never from the item.
 */

import { z } from 'zod';

import {
  ACTION_MESSAGE_MAX_IMAGES,
  ActionFileAttachmentsSchema,
  ActionMessageContentSchema,
  ActionMessageIdSchema,
  ActionProjectRefsSchema,
  ActionWireContractError,
  CanonicalIdentitySchema,
  ImageReferenceSchema,
} from '../actions/actionContracts';
import { canonicalTimestampSchema } from '../history/historyContracts';

/** `CHAT_PAGE_MAX_SIZE` (`routers/chat.py`). */
export const CHAT_PAGE_MAX_SIZE = 200;

const NonBlankTextSchema = z.string().refine((value) => value.trim().length > 0, 'text is blank');
const PositiveIntSchema = z.number().int().positive();

/** The body of `POST /chat/messages`; `message_id` is a user-wide idempotency key. */
export const ChatMessageRequestSchema = z
  .object({
    message_id: ActionMessageIdSchema,
    text: ActionMessageContentSchema,
    quote_item_id: ActionMessageIdSchema.nullable(),
    images: z.array(ImageReferenceSchema).max(ACTION_MESSAGE_MAX_IMAGES),
    files: ActionFileAttachmentsSchema,
    // Workspace projects named with @, in the Action message's shape: code-point spans into
    // the trimmed text. The backend checks each span names its project.
    project_refs: ActionProjectRefsSchema,
  })
  .strict();

/** Newest first; `before` is the previous page's `next_cursor`, or null for the newest page. */
export const ChatItemPageRequestSchema = z
  .object({
    before: PositiveIntSchema.nullable(),
    limit: z.number().int().min(1).max(CHAT_PAGE_MAX_SIZE),
  })
  .strict();

/** Run the turn that ended in this failure again. */
export const ChatTurnRetryRequestSchema = z
  .object({ failure_item_id: ActionMessageIdSchema })
  .strict();

const ChatTurnStateSchema = z.object({ running: z.boolean() }).strict();

const ChatCardSchema = z.discriminatedUnion('kind', [
  z
    .object({
      kind: z.literal('action'),
      action_id: CanonicalIdentitySchema,
      summary: NonBlankTextSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal('suggestion'),
      suggestion_id: CanonicalIdentitySchema,
      summary: NonBlankTextSchema,
    })
    .strict(),
]);

const ChatItemContentSchema = z.discriminatedUnion('kind', [
  z
    .object({
      kind: z.literal('user_message'),
      text: NonBlankTextSchema,
      quote_item_id: CanonicalIdentitySchema.nullable(),
      images: z.array(ImageReferenceSchema),
      files: ActionFileAttachmentsSchema,
      project_refs: ActionProjectRefsSchema,
    })
    .strict(),
  z
    .object({
      kind: z.literal('assistant_message'),
      text: NonBlankTextSchema,
      quote_item_id: CanonicalIdentitySchema.nullable(),
      cards: z.array(ChatCardSchema),
    })
    .strict(),
  z
    .object({ kind: z.literal('suggestion_event'), suggestion_id: CanonicalIdentitySchema })
    .strict(),
  z
    .object({
      kind: z.literal('action_event'),
      action_id: CanonicalIdentitySchema,
      event: z.enum(['completed', 'failed', 'canceled', 'approval_pending']),
      final_answer_excerpt: NonBlankTextSchema.nullable(),
    })
    .strict(),
  z
    .object({
      kind: z.literal('turn_failure'),
      reason: z.enum(['llm_connection', 'llm_request', 'step_limit', 'internal']),
    })
    .strict(),
]);

const ChatItemSchema = z
  .object({
    sequence: PositiveIntSchema,
    item_id: CanonicalIdentitySchema,
    created_at: canonicalTimestampSchema,
    content: ChatItemContentSchema,
  })
  .strict();

const ChatItemPageSchema = z
  .object({ items: z.array(ChatItemSchema), next_cursor: PositiveIntSchema.nullable() })
  .strict();

/** The request field a 400 names, so the composer can point at it. */
export const ChatMessageRejectedSchema = z
  .object({
    type: z.literal('ChatMessageRejected'),
    field: z.enum(['body', 'text', 'quote_item_id', 'images', 'files', 'project_refs']),
  })
  .strict();

export type ChatMessageRequest = z.infer<typeof ChatMessageRequestSchema>;
export type ChatItemPageRequest = z.infer<typeof ChatItemPageRequestSchema>;
export type ChatTurnRetryRequest = z.infer<typeof ChatTurnRetryRequestSchema>;
export type ChatTurnState = z.infer<typeof ChatTurnStateSchema>;
/** `stale`: another turn has ended since the failure, so there is nothing to retry. */
export type ChatTurnRetryResult = Readonly<{ kind: 'started' | 'stale' }>;
export type ChatCard = z.infer<typeof ChatCardSchema>;
export type ChatItemContent = z.infer<typeof ChatItemContentSchema>;
export type ChatItem = z.infer<typeof ChatItemSchema>;
export type ChatItemPage = z.infer<typeof ChatItemPageSchema>;
export type ChatMessageRejectedField = z.infer<typeof ChatMessageRejectedSchema>['field'];
export type ChatMessageSendResult =
  | Readonly<{ kind: 'sent'; item: ChatItem }>
  | Readonly<{ kind: 'rejected'; field: ChatMessageRejectedField }>;

export function parseChatItem(payload: unknown): ChatItem {
  const result = ChatItemSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('chat item');
  return result.data;
}

export function parseChatTurnState(payload: unknown): ChatTurnState {
  const result = ChatTurnStateSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('chat turn state');
  return result.data;
}

export function parseChatItemPage(payload: unknown): ChatItemPage {
  const result = ChatItemPageSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('chat page');
  return result.data;
}
