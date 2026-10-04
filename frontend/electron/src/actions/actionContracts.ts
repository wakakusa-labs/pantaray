import { z } from 'zod';

export const CanonicalIdentitySchema = z
  .string()
  .min(1)
  .refine((value) => value === value.trim(), 'identity must be canonical');
const NonBlankTextSchema = z.string().refine((value) => value.trim().length > 0, 'text is blank');
const CanonicalTimestampSchema = z.string().datetime({ precision: 6 });
const ActionStatusSchema = z.enum(['queued', 'processing', 'success', 'error', 'canceled']);
const RunStatusSchema = z.enum(['running', 'approval_pending', 'success', 'error', 'canceled']);
const ToolStatusSchema = z.enum(['processing', 'success', 'error', 'timeout']);
const TERMINAL_CONVERSATION_STATUSES = new Set(['success', 'error', 'canceled']);
const ACTION_MESSAGE_ID_MAX_CODEPOINTS = 128;
const ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS = 32_000;
const ACTION_MESSAGE_MAX_IMAGES = 32;
const ACTION_MESSAGE_MAX_PROJECT_REFS = 32;
/** Documents one message may carry; the local backend enforces the same limit. */
export const ACTION_MESSAGE_MAX_FILES = 10;
const ACTION_PROJECT_REF_MAX_PATHS = 32;
const ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS = 200;

export type ActionTimelinePosition = Readonly<{
  step_number: number;
  step_id: string;
}>;

export function compareActionTimelinePosition(
  left: ActionTimelinePosition,
  right: ActionTimelinePosition
): number {
  if (left.step_number !== right.step_number) return right.step_number - left.step_number;
  return left.step_id < right.step_id ? 1 : left.step_id > right.step_id ? -1 : 0;
}

function containsDuplicates(values: readonly (string | number)[]): boolean {
  return new Set(values).size !== values.length;
}

function boundedActionMessageText(maxCodePoints: number): z.ZodType<string> {
  return z
    .string()
    .transform((value) => value.trim())
    .refine((value) => value !== '', 'text is blank')
    .refine(
      (value) => Array.from(value).length <= maxCodePoints,
      'text exceeds the Unicode code-point limit'
    );
}

const ActionMessageIdSchema = boundedActionMessageText(ACTION_MESSAGE_ID_MAX_CODEPOINTS);
const ActionMessageContentSchema = boundedActionMessageText(ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS);

const ActionMessageTargetSchema = z.discriminatedUnion('kind', [
  z
    .object({
      kind: z.literal('new'),
      approval_mode: z.enum(['prompt_each_time', 'always_allow']).nullable().optional(),
      reply_to_suggestion_id: ActionMessageIdSchema.nullable().optional(),
    })
    .strict(),
  z
    .object({
      kind: z.literal('existing'),
      action_id: ActionMessageIdSchema,
      expected_process_id: ActionMessageIdSchema.nullable(),
    })
    .strict(),
]);

const ImageReferenceSchema = z
  .object({
    kind: z.literal('image'),
    storage_path: NonBlankTextSchema,
  })
  .strict();

const CodePointOffsetSchema = z.number().int().nonnegative();

// A document staged by `action:attachFile`. The backend moves the staged file into the
// Action's workspace on submit and re-validates the name and size against it.
export const ActionFileAttachmentsSchema = z
  .array(
    z
      .object({
        attachment_id: z.string().uuid(),
        name: NonBlankTextSchema,
        byte_size: z.number().int().positive(),
      })
      .strict()
  )
  .max(ACTION_MESSAGE_MAX_FILES);

// A workspace project named in the user's text, copied when the message is sent.
// start/end are Unicode code-point offsets into the trimmed text the backend
// stores; codePointSpanInTrimmedText converts the composer's UTF-16 offsets.
// The backend checks that each span names display_name and that paths are absolute.
export const ActionProjectRefsSchema = z
  .array(
    z
      .object({
        project_id: ActionMessageIdSchema,
        display_name: boundedActionMessageText(ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS),
        paths: z.array(NonBlankTextSchema).max(ACTION_PROJECT_REF_MAX_PATHS),
        start: CodePointOffsetSchema,
        end: CodePointOffsetSchema,
      })
      .strict()
  )
  .max(ACTION_MESSAGE_MAX_PROJECT_REFS);

/**
 * Convert a UTF-16 span of the raw composer draft to the code-point span of the
 * same characters in the trimmed text that is sent. This is the one place the two
 * units meet: JavaScript strings index UTF-16, the stored message indexes code points.
 */
export function codePointSpanInTrimmedText(
  draft: string,
  utf16Start: number,
  utf16End: number
): Readonly<{ start: number; end: number }> {
  const trimmedStart = draft.length - draft.trimStart().length;
  const codePointLength = (from: number, to: number) => Array.from(draft.slice(from, to)).length;
  const start = codePointLength(trimmedStart, utf16Start);
  return { start, end: start + codePointLength(utf16Start, utf16End) };
}

export const ActionMessageRequestSchema = z
  .object({
    target: ActionMessageTargetSchema,
    message: z
      .object({
        version: z.literal(1),
        message_id: ActionMessageIdSchema,
        content: ActionMessageContentSchema,
        images: z.array(ImageReferenceSchema).max(ACTION_MESSAGE_MAX_IMAGES),
        language: z.enum(['en', 'ja']).nullable().optional(),
        project_refs: ActionProjectRefsSchema.optional(),
        files: ActionFileAttachmentsSchema.optional(),
      })
      .strict(),
  })
  .strict();

/**
 * 「再開」は押しただけの操作で、送る本文がない。message_id だけが冪等キーとして
 * 要る（応答を取りこぼした再送が二つ目の run を開かないため）。
 */
export const ActionResumeRequestSchema = z
  .object({
    actionId: CanonicalIdentitySchema,
    messageId: ActionMessageIdSchema,
  })
  .strict();

const ActionMessageResponseBaseSchema = z
  .object({
    action_id: CanonicalIdentitySchema,
    message_id: CanonicalIdentitySchema,
    step_id: CanonicalIdentitySchema,
    action_status: ActionStatusSchema,
  })
  .strict();
const ActionMessageStartedResponseSchema = ActionMessageResponseBaseSchema.extend({
  disposition: z.literal('started'),
  process_id: CanonicalIdentitySchema,
});
const ActionMessageDeferredResponseSchema = ActionMessageResponseBaseSchema.extend({
  disposition: z.enum(['pending', 'not_executed']),
  process_id: z.null(),
});
const ActionMessageResponseSchema = z.union([
  ActionMessageStartedResponseSchema,
  ActionMessageDeferredResponseSchema,
]);

const ApprovedSuggestionSchema = z
  .object({ suggestion_id: CanonicalIdentitySchema, content: NonBlankTextSchema })
  .strict();

const UserEntrySchema = z
  .object({
    step_kind: z.literal('user'),
    step_id: CanonicalIdentitySchema,
    step_number: z.number().int().positive().nullable(),
    message_id: CanonicalIdentitySchema.nullable(),
    accepted_sequence: z.number().int().positive(),
    content: NonBlankTextSchema.nullable(),
    approved_suggestion: ApprovedSuggestionSchema.nullable(),
    images: z.array(ImageReferenceSchema),
    // Code-point spans of workspace projects named in `content`, as sent.
    project_refs: z.array(
      z
        .object({
          display_name: NonBlankTextSchema,
          start: CodePointOffsetSchema,
          end: CodePointOffsetSchema,
        })
        .strict()
    ),
    // Documents sent with the message. Optional until every backend returns the field.
    files: z
      .array(z.object({ name: z.string(), byte_size: z.number().int().positive() }).strict())
      .optional(),
    status: z.enum(['adopted', 'pending', 'not_executed']),
  })
  .strict()
  .superRefine((entry, context) => {
    if (entry.content === null && entry.approved_suggestion === null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'USER content is missing' });
    }
    if ((entry.status === 'adopted') !== (entry.step_number !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'USER timeline position is inconsistent',
      });
    }
    if (entry.status !== 'adopted' && entry.message_id === null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'message identity is required' });
    }
  });

const AssistantEntrySchema = z
  .object({
    step_kind: z.literal('assistant'),
    step_id: CanonicalIdentitySchema,
    step_number: z.number().int().positive(),
    content: NonBlankTextSchema,
  })
  .strict();

const ToolEntrySchema = z
  .object({
    step_kind: z.literal('tool'),
    step_id: CanonicalIdentitySchema,
    step_number: z.number().int().positive(),
    label: NonBlankTextSchema,
    status: ToolStatusSchema,
    // Whether the tool did what it was called for. A denied approval and a read taken
    // while recording is off are successful steps that never ran, and a call a Stop
    // reached before it was issued is an error step that never ran, so the status alone
    // cannot say. A page render whose renderer is still being set up succeeds without
    // drawing anything. A tool step recorded before the outcome existed did run.
    outcome: z
      .enum(['completed', 'denied', 'unavailable', 'not_executed', 'preparing'])
      .default('completed'),
    // The one argument the step acted on, and the head of its result when it has none.
    // A tool step recorded before the row said what it did carries neither.
    subject: NonBlankTextSchema.nullable().default(null),
    output_preview: NonBlankTextSchema.nullable().default(null),
    output_available: z.boolean(),
    // A tool step recorded before capture_screen existed carries no image list.
    images: z.array(ImageReferenceSchema).default([]),
  })
  .strict()
  .superRefine((entry, context) => {
    if (entry.status === 'processing' && entry.output_available) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'processing output is unavailable',
      });
    }
  });

const PublicActionErrorSchema = z
  .object({
    code: CanonicalIdentitySchema,
    message: NonBlankTextSchema,
  })
  .strict();

const ActionRunSchema = z
  .object({
    run_id: CanonicalIdentitySchema,
    status: RunStatusSchema,
    started_at: CanonicalTimestampSchema,
    completed_at: CanonicalTimestampSchema.nullable(),
    completion_event_id: CanonicalIdentitySchema.nullable(),
    entries: z.array(z.union([UserEntrySchema, AssistantEntrySchema, ToolEntrySchema])),
    final_output: NonBlankTextSchema.nullable(),
    error: PublicActionErrorSchema.nullable(),
  })
  .strict()
  .superRefine((run, context) => {
    const positions = run.entries.flatMap((entry) =>
      entry.step_number === null ? [] : [{ step_number: entry.step_number, step_id: entry.step_id }]
    );
    if (
      positions.length !== run.entries.length ||
      positions.some(
        (position, index) =>
          index > 0 && compareActionTimelinePosition(positions[index - 1], position) > 0
      )
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'run entries must follow the canonical timeline order',
      });
    }
    if (run.completed_at !== null && run.completed_at < run.started_at) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'run completion precedes its start',
      });
    }
    const outcome = [
      run.completed_at !== null,
      run.final_output !== null,
      run.error !== null,
      run.completion_event_id !== null,
    ] as const;
    const expected =
      run.status === 'success'
        ? ([true, true, false, true] as const)
        : run.status === 'error' || run.status === 'canceled'
          ? ([true, false, true, true] as const)
          : ([false, false, false, false] as const);
    if (outcome.some((value, index) => value !== expected[index])) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'run outcome is inconsistent' });
    }
  });

const ActionSummarySchema = z
  .object({
    action_id: CanonicalIdentitySchema,
    suggestion_id: CanonicalIdentitySchema.nullable(),
    approved_suggestion: ApprovedSuggestionSchema.nullable(),
    status: ActionStatusSchema,
    latest_run_id: CanonicalIdentitySchema.nullable(),
    // Whether the user's Stop is still this Action's latest intent, so the
    // composer offers 「再開」 instead of a disabled send.
    resumable: z.boolean(),
  })
  .strict()
  .superRefine((action, context) => {
    if (
      action.approved_suggestion !== null &&
      action.approved_suggestion.suggestion_id !== action.suggestion_id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'approved suggestion must belong to the Action',
      });
    }
    if ((action.status === 'queued' || action.status === 'processing') && !action.latest_run_id) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'latest run is required' });
    }
    if (action.resumable && action.status !== 'canceled') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'only a canceled Action is resumable',
      });
    }
  });

const ActionConversationPageSchema = z
  .object({
    action: ActionSummarySchema,
    runs: z.array(ActionRunSchema),
    unadopted_messages: z.array(UserEntrySchema),
    next_cursor: NonBlankTextSchema.nullable(),
  })
  .strict()
  .superRefine((page, context) => {
    const runEntries = page.runs.flatMap((run) => run.entries);
    if (
      page.unadopted_messages.some(
        (entry) =>
          (entry.status !== 'pending' && entry.status !== 'not_executed') ||
          (entry.status === 'pending' && TERMINAL_CONVERSATION_STATUSES.has(page.action.status))
      )
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'USER placement is inconsistent' });
    }

    const entries = [...runEntries, ...page.unadopted_messages];
    const userEntries = entries.filter((entry) => entry.step_kind === 'user');
    if (
      userEntries.some(
        (entry) =>
          entry.approved_suggestion !== null &&
          entry.approved_suggestion.suggestion_id !== page.action.suggestion_id
      )
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'approved suggestion must belong to the page Action',
      });
    }
    const identities = [
      entries.map((entry) => entry.step_id),
      userEntries.flatMap((entry) => (entry.message_id === null ? [] : [entry.message_id])),
      userEntries.map((entry) => entry.accepted_sequence),
      page.runs.map((run) => run.run_id),
    ];
    if (identities.some(containsDuplicates)) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'page identity is duplicated' });
    }

    const latestRun = page.runs.find((run) => run.run_id === page.action.latest_run_id);
    const orderedRuns = [...page.runs].sort((left, right) => {
      const leftKey = `${left.started_at}:${left.completed_at ?? 'Z'}`;
      const rightKey = `${right.started_at}:${right.completed_at ?? 'Z'}`;
      return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
    });
    const nonlatestRunIsInvalid = page.runs.some(
      (run) =>
        run.run_id !== page.action.latest_run_id &&
        (!TERMINAL_CONVERSATION_STATUSES.has(run.status) ||
          (latestRun !== undefined &&
            run.completed_at !== null &&
            run.completed_at > latestRun.started_at))
    );
    const runsOverlap = orderedRuns.some(
      (run, index) =>
        index < orderedRuns.length - 1 &&
        (run.completed_at === null || run.completed_at > orderedRuns[index + 1].started_at)
    );
    if (nonlatestRunIsInvalid || runsOverlap) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'visible runs must be terminal, ordered, and non-overlapping',
      });
    }

    const actionIsTerminal = TERMINAL_CONVERSATION_STATUSES.has(page.action.status);
    const latestRunIsTerminal =
      latestRun !== undefined && TERMINAL_CONVERSATION_STATUSES.has(latestRun.status);
    if (
      latestRun !== undefined &&
      ((latestRun.status === 'approval_pending' && page.action.status !== 'processing') ||
        actionIsTerminal !== latestRunIsTerminal ||
        (actionIsTerminal && latestRun.status !== page.action.status))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Action and latest run statuses do not agree',
      });
    }
  });

const ActionToolOutputDetailSchema = z
  .object({
    content: z.string(),
    next_cursor: NonBlankTextSchema.nullable(),
    truncated: z.boolean(),
    unavailable_reason: z.enum(['no_output', 'binary']).nullable(),
  })
  .strict()
  .superRefine((output, context) => {
    const unavailableShapeIsInvalid =
      output.unavailable_reason !== null &&
      (output.content !== '' || output.next_cursor !== null || output.truncated);
    const availableShapeIsInvalid =
      output.unavailable_reason === null &&
      ((!output.content && !output.truncated) || (output.next_cursor !== null && output.truncated));
    if (unavailableShapeIsInvalid || availableShapeIsInvalid) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'tool output is inconsistent' });
    }
  });

export type ActionMessageRequest = z.infer<typeof ActionMessageRequestSchema>;
export type ActionResumeRequest = z.infer<typeof ActionResumeRequestSchema>;
export type ActionMessageResponse = z.infer<typeof ActionMessageResponseSchema>;
export type ActionMessageSubmitResult =
  | Readonly<{ kind: 'submitted'; response: ActionMessageResponse }>
  | Readonly<{ kind: 'expected_process_conflict' }>
  | Readonly<{ kind: 'action_conflict' }>;
export type ActionConversationPage = z.infer<typeof ActionConversationPageSchema>;
export type ActionImageReference = z.infer<typeof ImageReferenceSchema>;
export type ActionProjectRef = z.infer<typeof ActionProjectRefsSchema>[number];
export type ActionFileAttachment = z.infer<typeof ActionFileAttachmentsSchema>[number];
export type ActionToolOutputDetail = z.infer<typeof ActionToolOutputDetailSchema>;

export class ActionWireContractError extends Error {
  constructor(contract: string) {
    super(`Local backend ${contract} response is invalid.`);
    this.name = 'ActionWireContractError';
  }
}

// The parsed shape is not the wire shape: a field the wire may omit gets its default here.
function parseResponse<T>(
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
  payload: unknown,
  contract: string
): T {
  const result = schema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError(contract);
  return result.data;
}

export function parseActionMessageResponse(payload: unknown): ActionMessageResponse {
  return parseResponse(ActionMessageResponseSchema, payload, 'Action message');
}

export function parseActionConversationPage(payload: unknown): ActionConversationPage {
  return parseResponse(ActionConversationPageSchema, payload, 'Action conversation');
}

export function parseActionToolOutputDetail(payload: unknown): ActionToolOutputDetail {
  return parseResponse(ActionToolOutputDetailSchema, payload, 'Action tool output');
}
