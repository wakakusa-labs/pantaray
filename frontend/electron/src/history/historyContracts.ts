import { z } from 'zod';

const HISTORY_MAX_PAGE_SIZE = 100;
const HISTORY_MAX_SEARCH_CODE_POINTS = 256;
const HISTORY_STATUS_LINE_MAX_CODE_POINTS = 120;

const canonicalIdentitySchema = z
  .string()
  .refine((value) => value.length > 0 && value === value.trim(), 'Invalid identity.');
const nonBlankTextSchema = z.string().refine((value) => value.trim().length > 0, 'Blank text.');
export const canonicalTimestampSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/)
  .refine((value) => {
    const parsed = new Date(value);
    return !Number.isNaN(parsed.valueOf()) && parsed.toISOString() === value;
  }, 'Invalid canonical timestamp.');

/** One plain line under a row's title, cut to fit by the runtime. */
const statusLineSchema = nonBlankTextSchema
  .refine(
    (value) => [...value].length <= HISTORY_STATUS_LINE_MAX_CODE_POINTS,
    'Status line is too long.'
  )
  .nullable();
/** The names of the files a row's task made or linked, in the order it shows them. */
const artifactNamesSchema = z.array(nonBlankTextSchema);

export const ConversationHistoryStatusSchema = z.enum(['running', 'approval_pending', 'idle']);

export const ConversationHistoryRequestSchema = z
  .object({
    cursor: z.string().nullable(),
    limit: z.number().int().min(1).max(HISTORY_MAX_PAGE_SIZE),
    filters: z
      .object({
        searchText: z
          .string()
          .refine(
            (value) => [...value].length <= HISTORY_MAX_SEARCH_CODE_POINTS,
            'Search text is too long.'
          ),
      })
      .strict(),
  })
  .strict();

const ConversationHistoryItemSchema = z
  .object({
    kind: z.literal('conversation'),
    action_id: canonicalIdentitySchema,
    title: nonBlankTextSchema,
    updated_at: canonicalTimestampSchema,
    status: ConversationHistoryStatusSchema,
    latest_completion_event_id: canonicalIdentitySchema.nullable(),
    status_line: statusLineSchema,
    artifact_names: artifactNamesSchema,
  })
  .strict();

const SuggestionHistoryItemSchema = z
  .object({
    kind: z.literal('suggestion'),
    suggestion_id: canonicalIdentitySchema,
    title: nonBlankTextSchema,
    updated_at: canonicalTimestampSchema,
    status: z.enum(['approval_pending', 'idle']),
    status_line: statusLineSchema,
    artifact_names: artifactNamesSchema,
  })
  .strict();

export const ConversationHistoryListItemSchema = z.discriminatedUnion('kind', [
  ConversationHistoryItemSchema,
  SuggestionHistoryItemSchema,
]);

export const ConversationHistoryPageSchema = z
  .object({
    items: z.array(ConversationHistoryListItemSchema),
    next_cursor: nonBlankTextSchema.nullable(),
  })
  .strict();

/** One history row, named the way `/api/agent/history/items/{kind}/{id}` names it. */
export const HistoryItemKeySchema = z
  .object({
    kind: z.enum(['conversation', 'suggestion']),
    id: canonicalIdentitySchema,
  })
  .strict();

export type ConversationHistoryStatus = z.infer<typeof ConversationHistoryStatusSchema>;
export type ConversationHistoryListItem = z.infer<
  typeof ConversationHistoryPageSchema
>['items'][number];
export type ConversationHistoryRequest = z.infer<typeof ConversationHistoryRequestSchema>;
export type HistoryItemKey = z.infer<typeof HistoryItemKeySchema>;
