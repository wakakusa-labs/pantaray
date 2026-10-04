/**
 * Zod schema for the `ws:send` IPC payload.
 *
 * Mirrors the `OrchestrationClientEvent` discriminated union in
 * `orchestration/eventContracts.ts`. The envelope (`event` field) is
 * strict so unknown event types are rejected before they reach the WS
 * connection. Per-variant `data` objects use Zod's default strip-unknown,
 * tolerating forward-compatible additions from the renderer.
 */

import { z } from 'zod';
import {
  ActionFileAttachmentsSchema,
  ActionMessageRequestSchema,
  ActionProjectRefsSchema,
} from '../../actions/actionContracts';

import {
  isActionSupplementWithinLimit,
  normalizeActionSupplement,
} from '../../orchestration/eventContracts';
import { MAX_ID_LENGTH, MAX_LAST_CHUNK_INDEX, MAX_REASON_LENGTH } from './limits';

const IdString = z.string().trim().min(1).max(MAX_ID_LENGTH);
const ReasonString = z.string().max(MAX_REASON_LENGTH);
const ProcessKindSchema = z.enum(['suggestion', 'action']);

const DismissSuggestionSchema = z.object({
  event: z.literal('dismiss_suggestion'),
  data: z.object({
    suggestion_id: IdString,
    reason: ReasonString.optional(),
  }),
});

const RejectSuggestionSchema = z.object({
  event: z.literal('reject_suggestion'),
  data: z.object({
    suggestion_id: IdString,
    reason: ReasonString.optional(),
  }),
});

const StopProcessSchema = z.object({
  event: z.literal('stop_process'),
  data: z.object({
    process_id: IdString,
  }),
});

const ResumeProcessSchema = z.object({
  event: z.literal('resume_process'),
  data: z.object({
    kind: ProcessKindSchema.optional(),
    process_id: IdString.optional(),
    processId: IdString.optional(),
    suggestion_id: IdString.optional(),
    suggestionId: IdString.optional(),
    action_id: IdString.optional(),
    actionId: IdString.optional(),
    command_id: IdString.optional(),
    commandId: IdString.optional(),
    fromStart: z.boolean().optional(),
  }),
});

const ResumeSessionSchema = z.object({
  event: z.literal('resume_session'),
  data: z.object({
    session_id: IdString,
    last_cursor: z.string().max(MAX_ID_LENGTH).nullable(),
    process_id: IdString,
    last_chunk_index: z.number().int().min(0).max(MAX_LAST_CHUNK_INDEX),
    kind: ProcessKindSchema,
    suggestion_id: IdString.optional(),
    action_id: IdString.optional(),
    command_id: IdString.optional(),
  }),
});

const AckEventSchema = z.object({
  event: z.literal('ack_event'),
  data: z.object({
    session_id: IdString,
    process_id: IdString,
    event_id: IdString,
  }),
});

export const OrchestrationClientEventSchema = z.discriminatedUnion('event', [
  DismissSuggestionSchema,
  RejectSuggestionSchema,
  StopProcessSchema,
  ResumeProcessSchema,
  ResumeSessionSchema,
  AckEventSchema,
]);

const ActionSupplementSchema = z
  .string()
  .transform(normalizeActionSupplement)
  .refine((value) => value === null || isActionSupplementWithinLimit(value))
  .nullable();

export const AcceptActionRequestSchema = z
  .object({
    suggestionId: IdString,
    commandId: IdString.nullable(),
    supplement: ActionSupplementSchema,
    supplementProjectRefs: ActionProjectRefsSchema,
    approvalMode: z.enum(['prompt_each_time', 'always_allow']),
    images: ActionMessageRequestSchema.shape.message.shape.images,
    files: ActionFileAttachmentsSchema.optional(),
  })
  .strict();
