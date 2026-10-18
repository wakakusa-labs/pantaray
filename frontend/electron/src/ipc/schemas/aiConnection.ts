import { z } from 'zod';
import {
  ApiKeyProviderSchema,
  CHATGPT_MODEL_CANDIDATES,
  ConnectionPreferencesSchema,
} from '../../aiConnection/preferences';
import type { ChatgptSignInError } from '../../auth/chatgptLogin';
import type { LocalConnectionRuntime } from '../../aiConnection/localConnectionRuntime';
import type { LocalConnectionStatus } from '../../auth/localBackendSessionSync';
import type { CredentialStorageError } from '../../auth/encryptedJsonFile';

const Key = z.string().trim().min(1);
export const ConnectionCommandSchema = z.discriminatedUnion('operation', [
  z
    .object({
      operation: z.literal('save_preferences'),
      preferences: ConnectionPreferencesSchema.refine(
        ({ method, model }) => method !== 'chatgpt' || CHATGPT_MODEL_CANDIDATES.includes(model)
      ),
    })
    .strict(),
  z
    .object({ operation: z.literal('save_api_key'), provider: ApiKeyProviderSchema, apiKey: Key })
    .strict(),
  z.object({ operation: z.literal('remove_api_key'), provider: ApiKeyProviderSchema }).strict(),
  z.object({ operation: z.literal('save_web_search_key'), apiKey: Key }).strict(),
  z.object({ operation: z.literal('remove_web_search_key') }).strict(),
  z.object({ operation: z.literal('save_integration_key'), apiKey: Key }).strict(),
  z.object({ operation: z.literal('remove_integration_key') }).strict(),
  z.object({ operation: z.literal('sign_in_chatgpt') }).strict(),
  z.object({ operation: z.literal('cancel_chatgpt_sign_in') }).strict(),
  z.object({ operation: z.literal('disconnect_chatgpt') }).strict(),
]);
export type ConnectionCommand = z.infer<typeof ConnectionCommandSchema>;
export type ConnectionRecoveryCommand = Extract<
  ConnectionCommand,
  {
    operation:
      | 'save_preferences'
      | 'remove_api_key'
      | 'remove_web_search_key'
      | 'remove_integration_key'
      | 'disconnect_chatgpt';
  }
>;
export type ConnectionSettings = Awaited<ReturnType<LocalConnectionRuntime['getSettings']>>;
export type ConnectionErrorCode =
  | CredentialStorageError['code']
  | 'invalid_input'
  | 'runtime_unavailable'
  | ChatgptSignInError;
export type ConnectionFailure = { ok: false; error: ConnectionErrorCode };
export type ConnectionRuntimeResult =
  | { ok: true; status: LocalConnectionStatus }
  | { ok: false; error: 'runtime_unavailable' };
export type ConnectionStateResult =
  | { ok: true; settings: ConnectionSettings; runtime: ConnectionRuntimeResult }
  | (ConnectionFailure & { recovery?: ConnectionRecoveryCommand });
export type ConnectionUpdateResult = { ok: true } | ConnectionFailure;
