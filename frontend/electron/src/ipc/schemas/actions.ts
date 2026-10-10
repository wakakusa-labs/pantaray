import { z } from 'zod';

import {
  ActionMessageRequestSchema,
  ActionResumeRequestSchema,
} from '../../actions/actionContracts';

export const ACTION_CONVERSATION_MAX_PAGE_SIZE = 100;
const ACTION_TOOL_OUTPUT_MIN_PAGE_BYTES = 4;
const ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES = 65_536;

const CanonicalIdentitySchema = z
  .string()
  .min(1)
  .refine((value) => value === value.trim(), 'identity must be canonical');
const OpaqueCursorSchema = z
  .string()
  .min(1)
  .refine((value) => value === value.trim(), 'cursor must be canonical')
  .nullable();

export { ActionMessageRequestSchema, ActionResumeRequestSchema };

export const ActionConversationPageRequestSchema = z
  .object({
    actionId: CanonicalIdentitySchema,
    cursor: OpaqueCursorSchema,
    limit: z.number().int().min(1).max(ACTION_CONVERSATION_MAX_PAGE_SIZE),
  })
  .strict();

export const ActionConversationOverlayRequestSchema = z
  .object({ actionId: CanonicalIdentitySchema })
  .strict();

export const ActionToolOutputRequestSchema = z
  .object({
    actionId: CanonicalIdentitySchema,
    stepId: CanonicalIdentitySchema,
    cursor: OpaqueCursorSchema,
    limitBytes: z
      .number()
      .int()
      .min(ACTION_TOOL_OUTPUT_MIN_PAGE_BYTES)
      .max(ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES),
  })
  .strict();
