/** Connected external apps' settings from the local backend (`routers/integrations.py`). */

import { z } from 'zod';

import { LocalBackendRequestError, type createLocalBackendClient } from '../localBackend/client';

const IntegrationAccessSchema = z.enum(['off', 'read_only', 'read_write']);

/** Why the API does not allow something; the settings screen localizes it. */
const ApiDenialSchema = z.strictObject({
  source: z.enum(['connection_method', 'account_grant']),
  capability: z.enum(['connect', 'execute', 'tool']),
  detail: z.string().nullable(),
});

const AccountStatusSchema = z.strictObject({
  account: z.string(),
  label: z.string(),
  health: z.enum(['ok', 'not_connected', 'reauthorization_required', 'unavailable']),
  access: IntegrationAccessSchema,
  options: z.array(
    z.strictObject({
      value: IntegrationAccessSchema,
      allowed: z.boolean(),
      reason: ApiDenialSchema.nullable(),
    })
  ),
});

const ToolkitStatusSchema = z.strictObject({
  toolkit: z.string(),
  display_name: z.string(),
  accounts: z.array(AccountStatusSchema),
});

// `limits` is left unparsed: it serves the connect flow, which this screen does not have yet.
const IntegrationsStatusSchema = z.discriminatedUnion('state', [
  z.strictObject({ state: z.literal('not_configured') }),
  z.object({ state: z.literal('configured'), toolkits: z.array(ToolkitStatusSchema) }),
]);

const IntegrationToolSettingsSchema = z.array(
  z.strictObject({
    slug: z.string(),
    name: z.string(),
    tool_class: z.enum(['read', 'write']),
    blocked: z.boolean(),
    api_denial: ApiDenialSchema.nullable(),
  })
);

const AccessNotAllowedPayloadSchema = z.object({ detail: z.object({ reason: ApiDenialSchema }) });

const NonEmptyStringSchema = z.string().min(1);

export const IntegrationAccountRefSchema = z.strictObject({
  toolkit: NonEmptyStringSchema,
  account: NonEmptyStringSchema,
});

export const IntegrationAccessUpdateSchema = IntegrationAccountRefSchema.extend({
  access: IntegrationAccessSchema,
});

export const IntegrationToolBlockUpdateSchema = IntegrationAccountRefSchema.extend({
  slug: NonEmptyStringSchema,
  blocked: z.boolean(),
});

export type IntegrationAccess = z.infer<typeof IntegrationAccessSchema>;
export type ApiDenial = z.infer<typeof ApiDenialSchema>;
export type IntegrationAccountStatus = z.infer<typeof AccountStatusSchema>;
export type IntegrationToolkitStatus = z.infer<typeof ToolkitStatusSchema>;
export type IntegrationsStatus = z.infer<typeof IntegrationsStatusSchema>;
export type IntegrationToolSetting = z.infer<typeof IntegrationToolSettingsSchema>[number];
export type IntegrationAccountRef = z.infer<typeof IntegrationAccountRefSchema>;
export type IntegrationAccessUpdate = z.infer<typeof IntegrationAccessUpdateSchema>;
export type IntegrationToolBlockUpdate = z.infer<typeof IntegrationToolBlockUpdateSchema>;

/** A refused option is an answer, not a failure: the screen shows its reason. */
export type IntegrationAccessUpdateResult =
  | { kind: 'saved'; account: IntegrationAccountStatus }
  | { kind: 'not_allowed'; reason: ApiDenial };

export type IntegrationSettingsApi = ReturnType<typeof createIntegrationSettingsFetcher>;

export function createIntegrationSettingsFetcher(params: {
  requestJson: ReturnType<typeof createLocalBackendClient>['requestJson'];
  getUserId: () => string | null;
}) {
  const integrationsPath = (): string => {
    const userId = params.getUserId();
    if (!userId) throw new Error('Missing authenticated user id.');
    return `/v1/agents/users/${encodeURIComponent(userId)}/integrations`;
  };
  const accountPath = (ref: IntegrationAccountRef): string =>
    `${integrationsPath()}/${encodeURIComponent(ref.toolkit)}/accounts/${encodeURIComponent(ref.account)}`;

  return {
    getStatus: async (): Promise<IntegrationsStatus> =>
      IntegrationsStatusSchema.parse(
        await params.requestJson<unknown>({ path: integrationsPath(), method: 'GET' })
      ),
    setAccountAccess: async (
      update: IntegrationAccessUpdate
    ): Promise<IntegrationAccessUpdateResult> => {
      let payload: unknown;
      try {
        payload = await params.requestJson<unknown>({
          path: `${accountPath(update)}/access`,
          method: 'PUT',
          body: { access: update.access },
        });
      } catch (error) {
        if (error instanceof LocalBackendRequestError && error.status === 409) {
          const refused = AccessNotAllowedPayloadSchema.safeParse(error.payload);
          if (refused.success) return { kind: 'not_allowed', reason: refused.data.detail.reason };
        }
        throw error;
      }
      return { kind: 'saved', account: AccountStatusSchema.parse(payload) };
    },
    listAccountTools: async (ref: IntegrationAccountRef): Promise<IntegrationToolSetting[]> =>
      IntegrationToolSettingsSchema.parse(
        await params.requestJson<unknown>({ path: `${accountPath(ref)}/tools`, method: 'GET' })
      ),
    setToolBlocked: async (update: IntegrationToolBlockUpdate): Promise<void> => {
      await params.requestJson<unknown>({
        path: `${accountPath(update)}/tools/${encodeURIComponent(update.slug)}/block`,
        method: update.blocked ? 'PUT' : 'DELETE',
      });
    },
  };
}
