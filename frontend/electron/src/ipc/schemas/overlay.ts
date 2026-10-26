/**
 * Zod schemas for the overlay IPC payloads.
 *
 * Shapes mirror `SubmitApprovalDecisionPayload` in
 * `actions/actionApprovalDecisionFetch.ts` and the Action approval mode
 * contract in `actions/actionApprovalModeFetch.ts`.
 */

import { z } from 'zod';

import { OVERLAY_APPROVAL_DECISIONS } from '../../actions/actionApprovalDecisionFetch';
import { CanonicalIdentitySchema } from '../../actions/actionContracts';
import { MAX_PATH_LENGTH } from './limits';
import { IdSchema } from './workspaceSettings';

export const SubmitApprovalDecisionPayloadSchema = z
  .object({
    actionId: IdSchema,
    processId: IdSchema,
    approvalSessionId: IdSchema,
    toolRequestId: IdSchema,
    decision: z.enum(OVERLAY_APPROVAL_DECISIONS),
  })
  .strict();

export const ActionApprovalModeUpdateSchema = z
  .object({
    actionId: IdSchema,
    approvalMode: z.enum(['prompt_each_time', 'always_allow']),
  })
  .strict();

/**
 * The work a panel hands to the main window: an Action, or a suggestion no Action took up yet,
 * with one of the suggestion's files to preview when its chip was pressed.
 */
export const OverlayOpenTaskRequestSchema = z
  .object({
    kind: z.enum(['action', 'suggestion']),
    id: CanonicalIdentitySchema,
    file: z.string().min(1).max(MAX_PATH_LENGTH).optional(),
  })
  .strict();

export type OverlayOpenTaskRequest = z.infer<typeof OverlayOpenTaskRequestSchema>;

/** The main window's request to show a suggestion in a panel. */
export const SuggestionOverlayOpenRequestSchema = z
  .object({
    suggestionId: CanonicalIdentitySchema,
    initialUiState: z.object({ expand: z.boolean().optional() }).strict().nullable().optional(),
    fromStart: z.boolean().optional(),
  })
  .strict();

export type SuggestionOverlayOpenRequest = z.infer<typeof SuggestionOverlayOpenRequestSchema>;
