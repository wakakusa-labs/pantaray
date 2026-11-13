/**
 * A TODO import as Electron main starts, reads, edits, registers and cancels it on the local
 * backend (`pantaray_agents/schema/todo_imports.py`) for the main window.
 *
 * The view is the draft as the workspace sees it now: each group's names, marked new or
 * existing, and its TODOs, marked when they repeat an open one. A change to it moves the same
 * counter as the TODOs, so `todos:changed` is the cue to read it again.
 */

import { z } from 'zod';

import {
  ACTION_MESSAGE_MAX_IMAGES,
  ActionFileAttachmentsSchema,
  ActionWireContractError,
  ImageReferenceSchema,
} from '../actions/actionContracts';

/** `ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS`: the import's Action takes the text as its first message. */
const IMPORT_TEXT_MAX_CODEPOINTS = 32_000;
/** `_Id` (`schema/todos.py`). */
const ORGANIZATION_ID_MAX_CODEPOINTS = 128;

const LocalDateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
/** Python counts a `str`'s length in code points, not UTF-16 units. */
const withinCodePoints = (maxCodepoints: number) => (value: string) =>
  Array.from(value).length <= maxCodepoints;
const ImportIdSchema = z.string().uuid();
const RevisionSchema = z.number().int().positive();
/** Whether a name is blank once trimmed is the backend's to decide. */
const DraftNameSchema = z.string().min(1).nullable();

const DraftTodoSchema = z
  .object({
    text: z.string().refine(withinCodePoints(IMPORT_TEXT_MAX_CODEPOINTS), 'text is too long'),
    due: LocalDateSchema.nullable().optional(),
    // Unset unless the source gives a start date, so the AI waits for one.
    starts_on: LocalDateSchema.nullable().optional(),
    // Register it even though its project already has an open TODO of this text.
    keep_duplicate: z.boolean().optional(),
  })
  .strict();

/** The TODOs of one project, or of no project when `project` is null. */
const DraftGroupSchema = z
  .object({
    organization: DraftNameSchema,
    project: DraftNameSchema,
    todos: z.array(DraftTodoSchema),
  })
  .strict();

const TodoImportDraftSchema = z.object({ groups: z.array(DraftGroupSchema) }).strict();

/**
 * The body of `POST /todo-imports`: the pasted text, the files `action:attachFile` staged, the
 * images `action:attachImage` wrote, or any of them together. `import_id` is the idempotency
 * key; `organization_id` is given exactly when `scope` is `organization`.
 */
export const TodoImportStartRequestSchema = z
  .object({
    import_id: ImportIdSchema,
    scope: z.enum(['all', 'organization', 'no_organization']),
    organization_id: z
      .string()
      .min(1)
      .refine(withinCodePoints(ORGANIZATION_ID_MAX_CODEPOINTS), 'id is too long')
      .optional(),
    text: z
      .string()
      .refine(withinCodePoints(IMPORT_TEXT_MAX_CODEPOINTS), 'text is too long')
      .nullable()
      .optional(),
    files: ActionFileAttachmentsSchema.optional(),
    images: z.array(ImageReferenceSchema).max(ACTION_MESSAGE_MAX_IMAGES).optional(),
  })
  .strict()
  .refine(
    (request) => (request.scope === 'organization') === (request.organization_id !== undefined),
    'organization_id is required exactly for scope=organization'
  )
  .refine(
    (request) =>
      (request.text ?? '').trim() !== '' ||
      (request.files ?? []).length > 0 ||
      (request.images ?? []).length > 0,
    'text, files or images are required'
  );

/** Names one import: reading and canceling carry nothing else. */
export const TodoImportRefRequestSchema = z.object({ import_id: ImportIdSchema }).strict();

/** The whole draft, written over the revision the user saw. */
export const TodoImportWriteRequestSchema = z
  .object({
    import_id: ImportIdSchema,
    expected_revision: RevisionSchema,
    draft: TodoImportDraftSchema,
  })
  .strict();

/** The revision the user confirmed. */
export const TodoImportRegisterRequestSchema = z
  .object({ import_id: ImportIdSchema, expected_revision: RevisionSchema })
  .strict();

const DraftIssueReasonSchema = z.enum([
  'organization_not_found',
  'organization_outside_scope',
  'project_required',
  'project_name_taken',
  'invalid_text',
  'too_many_todos',
]);

const DraftNameViewSchema = z.object({ name: z.string(), new: z.boolean() }).strict();

const TodoImportViewSchema = z
  .object({
    import_id: z.string(),
    scope: z.enum(['all', 'organization', 'no_organization']),
    organization_id: z.string().nullable(),
    revision: z.number().int(),
    state: z.enum(['open', 'registered', 'canceled']),
    // The Action that drafts it; null only while a start that failed is retried.
    action_id: z.string().nullable(),
    groups: z.array(
      z
        .object({
          organization: DraftNameViewSchema.nullable(),
          project: DraftNameViewSchema.nullable(),
          todos: z.array(
            z
              .object({
                text: z.string(),
                due: LocalDateSchema.nullable(),
                starts_on: LocalDateSchema.nullable(),
                // Its project already has an open TODO of this text, or an earlier line does.
                duplicate: z.boolean(),
                keep_duplicate: z.boolean(),
              })
              .strict()
          ),
        })
        .strict()
    ),
    // Why the draft no longer registers, after the workspace changed under it.
    issue: z
      .object({
        reason: DraftIssueReasonSchema,
        group_index: z.number().int().nullable(),
        todo_index: z.number().int().nullable(),
      })
      .strict()
      .nullable(),
    result: z
      .object({
        organization_ids: z.array(z.string()),
        project_ids: z.array(z.string()),
        todo_ids: z.array(z.string()),
        skipped_duplicates: z.number().int(),
      })
      .strict()
      .nullable(),
  })
  .strict();

/**
 * An import neither registered nor canceled, as `GET /todo-imports` lists them for a scope, newest
 * first. `request` is the first line of what the user sent: the text, or the files named.
 */
const OpenTodoImportSchema = z
  .object({ import_id: z.string(), request: z.string(), started_at: z.string() })
  .strict();

/** `TodoImportRefused`: the indexes locate a draft issue. */
export const TodoImportRefusedSchema = z
  .object({
    type: z.literal('TodoImportRefused'),
    reason: z.union([
      DraftIssueReasonSchema,
      z.enum([
        'not_found',
        'stale_revision',
        'registered',
        'canceled',
        // A staged file is gone, an image is not one the app wrote, or another start already
        // used this import id.
        'files_unavailable',
        'images_invalid',
        'start_conflict',
      ]),
    ]),
    group_index: z.number().int().nullable(),
    todo_index: z.number().int().nullable(),
  })
  .strict();

export type TodoImportDraft = z.infer<typeof TodoImportDraftSchema>;
export type TodoImportStartRequest = z.infer<typeof TodoImportStartRequestSchema>;
export type TodoImportRefRequest = z.infer<typeof TodoImportRefRequestSchema>;
export type TodoImportWriteRequest = z.infer<typeof TodoImportWriteRequestSchema>;
export type TodoImportRegisterRequest = z.infer<typeof TodoImportRegisterRequestSchema>;
export type TodoImportView = z.infer<typeof TodoImportViewSchema>;
export type OpenTodoImport = z.infer<typeof OpenTodoImportSchema>;
export type TodoImportRefusal = Omit<z.infer<typeof TodoImportRefusedSchema>, 'type'>;
/** A request the backend refused keeps its reason; what the screen shows is its to decide. */
export type TodoImportResult =
  | Readonly<{ kind: 'ok'; result: TodoImportView }>
  | Readonly<{ kind: 'refused'; refusal: TodoImportRefusal }>;

export function parseTodoImportView(payload: unknown): TodoImportView {
  const result = TodoImportViewSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('TODO import');
  return result.data;
}

export function parseOpenTodoImports(payload: unknown): OpenTodoImport[] {
  const result = z.array(OpenTodoImportSchema).safeParse(payload);
  if (!result.success) throw new ActionWireContractError('open TODO imports');
  return result.data;
}
