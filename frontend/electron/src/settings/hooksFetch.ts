/** The schedules Actions set up with the user's yes, from the local backend (`routers/hooks.py`). */

import { z } from 'zod';

import type { createLocalBackendClient } from '../localBackend/client';

const InstantSchema = z.string().datetime({ offset: true });

const FieldMatchSchema = z.strictObject({
  field: z.string(),
  op: z.enum(['equals', 'contains']),
  values: z.array(z.string()),
});

/**
 * One schedule: once `at`, every calendar unit or duration (`offset` after each unit's start),
 * or on each event `on` (`activity`, `microphone`) whose fields hold every field of `match`,
 * which may be empty for `microphone`. It sends to its target
 * conversation, or with none starts a new conversation (a TODO titled `title`) each time.
 */
const HookSchema = z.strictObject({
  hook_id: z.string().min(1),
  at: InstantSchema.nullable(),
  every: z.string().nullable(),
  offset: z.string().nullable(),
  on: z.string().nullable(),
  match: z.array(FieldMatchSchema),
  until: InstantSchema.nullable(),
  message: z.string(),
  enabled: z.boolean(),
  next_fire_at: InstantSchema.nullable(),
  created_from_action_id: z.string().min(1),
  created_from_title: z.string().nullable(),
  target_action_id: z.string().min(1).nullable(),
  title: z.string().nullable(),
  organization_id: z.string().nullable(),
  project_id: z.string().nullable(),
  last_result: z.enum(['completed', 'failed', 'canceled', 'not_started']).nullable(),
  failure_count: z.number().int().nonnegative(),
  stopped_reason: z.enum(['start_failed', 'turn_failed']).nullable(),
});

const HookListSchema = z.strictObject({
  hooks: z.array(HookSchema),
  recording_ready: z.boolean(),
});

export const HookEnabledUpdateSchema = z.strictObject({
  hook_id: z.string().regex(/^[^/]+$/),
  enabled: z.boolean(),
});

export type HookFieldMatch = z.infer<typeof FieldMatchSchema>;
export type HookView = z.infer<typeof HookSchema>;
export type HookList = z.infer<typeof HookListSchema>;
export type HookEnabledUpdate = z.infer<typeof HookEnabledUpdateSchema>;

export type HooksApi = ReturnType<typeof createHooksFetcher>;

export function createHooksFetcher(params: {
  requestJson: ReturnType<typeof createLocalBackendClient>['requestJson'];
  getUserId: () => string | null;
}) {
  const hooksPath = (): string => {
    const userId = params.getUserId();
    if (!userId) throw new Error('Missing authenticated user id.');
    return `/v1/agents/users/${encodeURIComponent(userId)}/hooks`;
  };
  return {
    list: async (): Promise<HookList> =>
      HookListSchema.parse(await params.requestJson<unknown>({ path: hooksPath(), method: 'GET' })),
    setEnabled: async (update: HookEnabledUpdate): Promise<void> => {
      await params.requestJson<unknown>({
        path: `${hooksPath()}/${encodeURIComponent(update.hook_id)}/enabled`,
        method: 'PUT',
        body: { enabled: update.enabled },
      });
    },
  };
}
