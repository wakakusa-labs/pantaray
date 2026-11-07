/**
 * The TODO list and its progress as Electron main reads and writes them on the local backend
 * (`pantaray_agents/schema/todos.py`, `schema/todo_progress.py`) for the main window.
 *
 * A row's state is `state`, and its conversation is named by id only. `todos:changed` carries
 * no TODO: the screen reads the TODOs again.
 */

import { z } from 'zod';

import { ActionWireContractError } from '../actions/actionContracts';

/** `_Id` (`schema/todos.py`). */
const TODO_ID_MAX_CODEPOINTS = 128;
/** `ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS`: the pickup hands the text to an Action as its first message. */
const TODO_TEXT_MAX_CODEPOINTS = 32_000;
/** `GOAL_TEXT_MAX_LENGTH`, `GOAL_UNIT_MAX_LENGTH`, `RECORD_NOTE_MAX_LENGTH` (`schema/todo_progress.py`). */
const GOAL_TEXT_MAX_CODEPOINTS = 200;
const GOAL_UNIT_MAX_CODEPOINTS = 20;
const RECORD_NOTE_MAX_CODEPOINTS = 200;

/** The user's calendar date, YYYY-MM-DD; the backend owns the clock. */
const LocalDateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const TodoAuthorSchema = z.enum(['user', 'ai']);
/** Python counts a `str`'s length in code points, not UTF-16 units. */
const textSchema = (maxCodepoints: number) =>
  z
    .string()
    .min(1)
    .refine(
      (value) => Array.from(value).length <= maxCodepoints,
      'exceeds the Unicode code-point limit'
    );
const TodoIdSchema = textSchema(TODO_ID_MAX_CODEPOINTS);
/** `_Quantity`: positive and finite. */
const QuantitySchema = z.number().positive().finite();
const GoalUnitSchema = textSchema(GOAL_UNIT_MAX_CODEPOINTS);
/** `PeriodKind`: what one row of the progress view spans. */
const PeriodKindSchema = z.enum(['day', 'week', 'month']);

const TodoDueSchema = z
  .object({ date: LocalDateSchema, repeat: z.enum(['none', 'weekly']) })
  .strict();

const scopeShape = {
  scope: z.enum(['all', 'organization', 'no_organization']),
  organization_id: TodoIdSchema.optional(),
};

const projectShape = {
  project: z.enum(['any', 'project', 'no_project']),
  project_id: TodoIdSchema.optional(),
};

const scopeMatches = (query: { scope: string; organization_id?: string }): boolean =>
  (query.scope === 'organization') === (query.organization_id !== undefined);
const SCOPE_MISMATCH = 'organization_id is required exactly for scope=organization';
const projectMatches = (query: { project: string; project_id?: string }): boolean =>
  (query.project === 'project') === (query.project_id !== undefined);
const PROJECT_MISMATCH = 'project_id is required exactly for project=project';

/** `organization_id` is given exactly when `scope` is `organization`. */
export const TodoScopeQuerySchema = z
  .object(scopeShape)
  .strict()
  .refine(scopeMatches, SCOPE_MISMATCH);

/**
 * `project_id` is given exactly when `project` is `project`; no `week_start` is the current
 * week, and any day of a week names that week.
 */
export const TodoWeekQuerySchema = z
  .object({ ...scopeShape, ...projectShape, week_start: LocalDateSchema.optional() })
  .strict()
  .refine(scopeMatches, SCOPE_MISMATCH)
  .refine(projectMatches, PROJECT_MISMATCH);

/** The progress view's rows are the `period`s up to today; scope and project as for the week. */
export const TodoProgressQuerySchema = z
  .object({ ...scopeShape, ...projectShape, period: PeriodKindSchema })
  .strict()
  .refine(scopeMatches, SCOPE_MISMATCH)
  .refine(projectMatches, PROJECT_MISMATCH);

/**
 * The body of `POST /todos`. `client_todo_id` is the idempotency key; `week_start` may be any
 * day of the week. Whether the text is one non-blank line is the backend's to decide.
 */
export const TodoCreateRequestSchema = z
  .object({
    client_todo_id: TodoIdSchema,
    project_id: TodoIdSchema.nullable(),
    text: z
      .string()
      .refine(
        (value) => Array.from(value).length <= TODO_TEXT_MAX_CODEPOINTS,
        'text exceeds the Unicode code-point limit'
      ),
    week_start: LocalDateSchema,
    due: TodoDueSchema.nullable(),
  })
  .strict();

export const TodoCheckRequestSchema = z
  .object({ todo_id: TodoIdSchema, checked: z.boolean() })
  .strict();

export const TodoDueRequestSchema = z
  .object({ todo_id: TodoIdSchema, due: TodoDueSchema.nullable() })
  .strict();

/** Names one TODO: delete and restore carry nothing else. */
export const TodoRefRequestSchema = z.object({ todo_id: TodoIdSchema }).strict();

/** The answer to the AI's pending deletion request. */
export const TodoDeletionAnswerRequestSchema = z
  .object({
    todo_id: TodoIdSchema,
    request_id: TodoIdSchema,
    answer: z.enum(['approve', 'keep']),
  })
  .strict();

/**
 * Sets or edits the project's goal. Whether `deadline` precedes the day the goal was first set,
 * and whether the text is one non-blank line, is the backend's to decide.
 */
export const TodoGoalRequestSchema = z
  .object({
    project_id: TodoIdSchema,
    text: textSchema(GOAL_TEXT_MAX_CODEPOINTS),
    target: QuantitySchema,
    unit: GoalUnitSchema,
    deadline: LocalDateSchema,
  })
  .strict();

/** Names one project: clearing its goal carries nothing else. */
export const TodoProjectRefRequestSchema = z.object({ project_id: TodoIdSchema }).strict();

/** The user's own record; `client_record_id` is the idempotency key. */
export const ProgressRecordCreateRequestSchema = z
  .object({
    todo_id: TodoIdSchema,
    client_record_id: TodoIdSchema,
    quantity: QuantitySchema,
    unit: GoalUnitSchema,
    note: textSchema(RECORD_NOTE_MAX_CODEPOINTS),
  })
  .strict();

/** Names one progress record, the AI's too. */
export const ProgressRecordRefRequestSchema = z.object({ record_id: TodoIdSchema }).strict();

const TodoStateSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('done'), by: TodoAuthorSchema, at: z.string() }).strict(),
  z.object({ kind: z.literal('working'), note: z.string().nullable() }).strict(),
  z
    .object({
      kind: z.literal('waiting'),
      reason: z.enum(['approval', 'reply']),
      note: z.string().nullable(),
    })
    .strict(),
  z.object({ kind: z.literal('partial'), note: z.string().nullable() }).strict(),
  z.object({ kind: z.literal('stopped'), outcome: z.enum(['failed', 'canceled']) }).strict(),
  z
    .object({ kind: z.literal('not_started'), reason: z.enum(['week_not_started', 'starting']) })
    .strict(),
]);

const TodoConversationRefSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('none') }).strict(),
  z.object({ kind: z.literal('suggestion'), suggestion_id: z.string() }).strict(),
  z.object({ kind: z.literal('action'), action_id: z.string() }).strict(),
]);

const TodoViewSchema = z
  .object({
    todo_id: z.string(),
    project_id: z.string().nullable(),
    text: z.string(),
    author: TodoAuthorSchema,
    origin: z.enum(['written', 'suggestion', 'recurrence']),
    // The week it was written for.
    week_start: LocalDateSchema,
    // An open TODO from an earlier week, shown in the current one.
    carried: z.boolean(),
    due: TodoDueSchema.nullable(),
    state: TodoStateSchema,
    // Only while the request is pending.
    deletion_request: z.object({ request_id: z.string(), reason: z.string() }).strict().nullable(),
    conversation: TodoConversationRefSchema,
  })
  .strict();

const TodoCheckResultSchema = z
  .object({
    todo: TodoViewSchema,
    // The next week's instance of a checked weekly TODO.
    next_todo: TodoViewSchema.nullable(),
  })
  .strict();

const TodoWeekPageSchema = z
  .object({
    today: LocalDateSchema,
    current_week_start: LocalDateSchema,
    week: z.object({ start: LocalDateSchema, end: LocalDateSchema }).strict(),
    items: z.array(TodoViewSchema),
  })
  .strict();

const DeletedTodoViewSchema = z
  .object({
    todo_id: z.string(),
    project_id: z.string().nullable(),
    text: z.string(),
    deleted_by: z.enum(['user', 'ai_approved_by_user']),
    deleted_at: z.string(),
    // The local date from which it can no longer be restored.
    purge_on: LocalDateSchema,
  })
  .strict();

const TodoChangedSchema = z.object({ revision: z.number().int() }).strict();

const GoalViewSchema = z
  .object({
    project_id: z.string(),
    text: z.string(),
    target: z.number(),
    unit: z.string(),
    deadline: LocalDateSchema,
    // Records count from here.
    set_at: z.string(),
  })
  .strict();

const ProgressRecordEvidenceSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('user') }).strict(),
  z
    .object({
      kind: z.literal('action_steps'),
      action_id: z.string(),
      step_ids: z.array(z.string()),
    })
    .strict(),
]);

const ProgressRecordViewSchema = z
  .object({
    record_id: z.string(),
    todo_id: z.string(),
    project_id: z.string().nullable(),
    quantity: z.number(),
    unit: z.string(),
    note: z.string(),
    evidence: ProgressRecordEvidenceSchema,
    recorded_at: z.string(),
  })
  .strict();

const ProgressPeriodViewSchema = z
  .object({
    start: LocalDateSchema,
    end: LocalDateSchema,
    // "N 件進みました" is their count.
    record_ids: z.array(z.string()),
    checked: z.array(
      z
        .object({
          todo_id: z.string(),
          project_id: z.string().nullable(),
          text: z.string(),
          checked_at: z.string(),
        })
        .strict()
    ),
  })
  .strict();

const paceWindowShape = { window_start: LocalDateSchema, record_ids: z.array(z.string()) };

const ProgressPaceSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('too_few_records'), ...paceWindowShape }).strict(),
  z.object({ kind: z.literal('reached') }).strict(),
  z
    .object({
      kind: z.literal('projected'),
      ...paceWindowShape,
      per_day: z.number(),
      projected_on: LocalDateSchema,
    })
    .strict(),
  // The pace reaches the target too far ahead to name a day.
  z.object({ kind: z.literal('beyond_horizon'), ...paceWindowShape, per_day: z.number() }).strict(),
]);

const GoalProgressSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('no_goal'), project_id: z.string() }).strict(),
  z
    .object({
      kind: z.literal('no_records'),
      goal: GoalViewSchema,
      other_unit_record_ids: z.array(z.string()),
    })
    .strict(),
  z
    .object({
      kind: z.literal('measured'),
      goal: GoalViewSchema,
      total: z.number(),
      share: z.number(),
      record_ids: z.array(z.string()),
      other_unit_record_ids: z.array(z.string()),
      expected_share: z.number(),
      deadline_passed: z.boolean(),
      pace: ProgressPaceSchema,
    })
    .strict(),
]);

const TodoProgressPageSchema = z
  .object({
    today: LocalDateSchema,
    period: PeriodKindSchema,
    // Newest first.
    rows: z.array(ProgressPeriodViewSchema),
    goals: z.array(GoalProgressSchema),
    // Every record an id above names.
    records: z.array(ProgressRecordViewSchema),
  })
  .strict();

/** The `error_code`s the TODO routes refuse with (`routers/todos.py`, `routers/todo_progress.py`). */
export const TODO_REFUSAL_CODES = [
  'TODO_NOT_FOUND',
  'TODO_DELETED',
  'TODO_BUSY',
  'TODO_REQUEST_STALE',
  'TODO_RESTORE_EXPIRED',
  'TODO_CLIENT_ID_CONFLICT',
  'TODO_INVALID_TEXT',
  'PROJECT_NOT_FOUND',
  'TODO_GOAL_DEADLINE_BEFORE_SET',
  'TODO_PROGRESS_INVALID_TEXT',
  'TODO_PROGRESS_CLIENT_ID_CONFLICT',
] as const;

export type TodoDue = z.infer<typeof TodoDueSchema>;
export type TodoScopeQuery = z.infer<typeof TodoScopeQuerySchema>;
export type TodoWeekQuery = z.infer<typeof TodoWeekQuerySchema>;
export type TodoCreateRequest = z.infer<typeof TodoCreateRequestSchema>;
export type TodoCheckRequest = z.infer<typeof TodoCheckRequestSchema>;
export type TodoDueRequest = z.infer<typeof TodoDueRequestSchema>;
export type TodoRefRequest = z.infer<typeof TodoRefRequestSchema>;
export type TodoDeletionAnswerRequest = z.infer<typeof TodoDeletionAnswerRequestSchema>;
export type TodoState = z.infer<typeof TodoStateSchema>;
export type TodoConversationRef = z.infer<typeof TodoConversationRefSchema>;
export type TodoView = z.infer<typeof TodoViewSchema>;
export type TodoCheckResult = z.infer<typeof TodoCheckResultSchema>;
export type TodoWeekPage = z.infer<typeof TodoWeekPageSchema>;
export type DeletedTodoView = z.infer<typeof DeletedTodoViewSchema>;
export type TodoChanged = z.infer<typeof TodoChangedSchema>;
export type TodoProgressQuery = z.infer<typeof TodoProgressQuerySchema>;
export type TodoGoalRequest = z.infer<typeof TodoGoalRequestSchema>;
export type TodoProjectRefRequest = z.infer<typeof TodoProjectRefRequestSchema>;
export type ProgressRecordCreateRequest = z.infer<typeof ProgressRecordCreateRequestSchema>;
export type ProgressRecordRefRequest = z.infer<typeof ProgressRecordRefRequestSchema>;
export type GoalView = z.infer<typeof GoalViewSchema>;
export type ProgressRecordEvidence = z.infer<typeof ProgressRecordEvidenceSchema>;
export type ProgressRecordView = z.infer<typeof ProgressRecordViewSchema>;
export type ProgressPeriodView = z.infer<typeof ProgressPeriodViewSchema>;
export type ProgressPace = z.infer<typeof ProgressPaceSchema>;
export type GoalProgress = z.infer<typeof GoalProgressSchema>;
export type TodoProgressPage = z.infer<typeof TodoProgressPageSchema>;
export type TodoRefusalCode = (typeof TODO_REFUSAL_CODES)[number];
/** A write the backend refused keeps its code; which codes the screen shows is its to decide. */
export type TodoWriteResult<T> =
  | Readonly<{ kind: 'ok'; result: T }>
  | Readonly<{ kind: 'refused'; code: TodoRefusalCode }>;

function parseWith<T>(schema: z.ZodType<T>, contract: string, payload: unknown): T {
  const result = schema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError(contract);
  return result.data;
}

export const parseTodoView = (payload: unknown): TodoView =>
  parseWith(TodoViewSchema, 'TODO', payload);
export const parseTodoCheckResult = (payload: unknown): TodoCheckResult =>
  parseWith(TodoCheckResultSchema, 'TODO check', payload);
export const parseTodoWeekPage = (payload: unknown): TodoWeekPage =>
  parseWith(TodoWeekPageSchema, 'TODO week', payload);
export const parseDeletedTodos = (payload: unknown): DeletedTodoView[] =>
  parseWith(z.array(DeletedTodoViewSchema), 'deleted TODOs', payload);
export const parseTodoChanged = (payload: unknown): TodoChanged =>
  parseWith(TodoChangedSchema, 'TODO change', payload);
export const parseTodoProgressPage = (payload: unknown): TodoProgressPage =>
  parseWith(TodoProgressPageSchema, 'TODO progress', payload);
export const parseGoalView = (payload: unknown): GoalView =>
  parseWith(GoalViewSchema, 'TODO goal', payload);
export const parseProgressRecordView = (payload: unknown): ProgressRecordView =>
  parseWith(ProgressRecordViewSchema, 'progress record', payload);
