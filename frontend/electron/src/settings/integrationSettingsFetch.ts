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
  logo_url: z.string().nullable(),
  accounts: z.array(AccountStatusSchema),
});

// `limits` is left unparsed: a refused connect answers 409 with its own reason instead.
const IntegrationsStatusSchema = z.discriminatedUnion('state', [
  z.strictObject({ state: z.literal('not_configured') }),
  z.object({ state: z.literal('configured'), toolkits: z.array(ToolkitStatusSchema) }),
  z.strictObject({ state: z.literal('key_rejected') }),
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

const NotAllowedPayloadSchema = z.object({ detail: z.object({ reason: ApiDenialSchema }) });

/** A connect link: opened in the browser, never logged, since it carries the link's token. */
const ConnectStartedSchema = z.strictObject({
  redirect_url: z.string().url(),
  expires_at: z.string().datetime({ offset: true }),
  connection_id: z.string().min(1),
});

const ConnectionStateSchema = z.strictObject({
  state: z.enum(['pending', 'active', 'failed', 'expired']),
});

/** Each value is one segment of a backend path. */
const PathSegmentSchema = z.string().regex(/^[^/]+$/);

export const IntegrationToolkitRefSchema = z.strictObject({ toolkit: PathSegmentSchema });

export const IntegrationAccountRefSchema = IntegrationToolkitRefSchema.extend({
  account: PathSegmentSchema,
});

export const IntegrationConnectionRefSchema = z.strictObject({
  connection_id: PathSegmentSchema,
});

export const IntegrationAccessUpdateSchema = IntegrationAccountRefSchema.extend({
  access: IntegrationAccessSchema,
});

export const IntegrationToolBlockUpdateSchema = IntegrationAccountRefSchema.extend({
  slug: PathSegmentSchema,
  blocked: z.boolean(),
});

export type IntegrationAccess = z.infer<typeof IntegrationAccessSchema>;
export type ApiDenial = z.infer<typeof ApiDenialSchema>;
export type IntegrationAccountStatus = z.infer<typeof AccountStatusSchema>;
export type IntegrationToolkitStatus = z.infer<typeof ToolkitStatusSchema>;
export type IntegrationsStatus = z.infer<typeof IntegrationsStatusSchema>;
export type IntegrationToolSetting = z.infer<typeof IntegrationToolSettingsSchema>[number];
export type IntegrationToolkitRef = z.infer<typeof IntegrationToolkitRefSchema>;
export type IntegrationAccountRef = z.infer<typeof IntegrationAccountRefSchema>;
export type IntegrationConnectionRef = z.infer<typeof IntegrationConnectionRefSchema>;
export type IntegrationConnectionState = z.infer<typeof ConnectionStateSchema>['state'];
export type IntegrationAccessUpdate = z.infer<typeof IntegrationAccessUpdateSchema>;
export type IntegrationToolBlockUpdate = z.infer<typeof IntegrationToolBlockUpdateSchema>;

/** A refused option is an answer, not a failure: the screen shows its reason. */
export type IntegrationAccessUpdateResult =
  | { kind: 'saved'; account: IntegrationAccountStatus }
  | { kind: 'not_allowed'; reason: ApiDenial };

/** Connecting is refused the same way an access option is: by a typed reason. */
export type IntegrationConnectResult =
  | ({ kind: 'started' } & z.infer<typeof ConnectStartedSchema>)
  | { kind: 'not_allowed'; reason: ApiDenial };

/** Returns the 409's reason, or null when the error is anything else. */
function notAllowedReason(error: unknown): ApiDenial | null {
  if (!(error instanceof LocalBackendRequestError) || error.status !== 409) return null;
  const refused = NotAllowedPayloadSchema.safeParse(error.payload);
  return refused.success ? refused.data.detail.reason : null;
}

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

  const startConnect = async (path: string): Promise<IntegrationConnectResult> => {
    let payload: unknown;
    try {
      payload = await params.requestJson<unknown>({ path, method: 'POST', body: {} });
    } catch (error) {
      const reason = notAllowedReason(error);
      if (reason) return { kind: 'not_allowed', reason };
      throw error;
    }
    return { kind: 'started', ...ConnectStartedSchema.parse(payload) };
  };

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
        const reason = notAllowedReason(error);
        if (reason) return { kind: 'not_allowed', reason };
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
    connect: (ref: IntegrationToolkitRef): Promise<IntegrationConnectResult> =>
      startConnect(`${integrationsPath()}/${encodeURIComponent(ref.toolkit)}/connect`),
    reconnect: (ref: IntegrationAccountRef): Promise<IntegrationConnectResult> =>
      startConnect(`${accountPath(ref)}/reconnect`),
    disconnect: async (ref: IntegrationAccountRef): Promise<void> => {
      await params.requestJson<unknown>({ path: accountPath(ref), method: 'DELETE' });
    },
    getConnectionState: async (
      ref: IntegrationConnectionRef
    ): Promise<IntegrationConnectionState> =>
      ConnectionStateSchema.parse(
        await params.requestJson<unknown>({
          path: `${integrationsPath()}/connections/${encodeURIComponent(ref.connection_id)}`,
          method: 'GET',
        })
      ).state,
  };
}
