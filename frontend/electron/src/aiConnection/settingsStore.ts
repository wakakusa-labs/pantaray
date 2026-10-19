import path from 'node:path';
import { z } from 'zod';
import {
  createEncryptedJsonFile,
  CredentialStorageError,
  type CredentialEncryption,
} from '../auth/encryptedJsonFile';
import {
  ConnectionPreferencesSchema,
  EMPTY_CONNECTION_PREFERENCES,
  type ApiKeyProvider,
  type ConnectionPreferences,
} from './preferences';

/** Device-local connection settings. Only main may read the credentials from this store. */
export function createConnectionSettingsStore(params: {
  userDataDir: string;
  safeStorage: CredentialEncryption;
}) {
  const file = (name: string) =>
    createEncryptedJsonFile(path.join(params.userDataDir, name), params.safeStorage);
  const preferencesFile = file('ai-connection-preferences.enc.json');
  const webSearchFile = file('tavily-api-key.enc.json');
  const integrationFile = file('composio-api-key.enc.json');
  const apiFile = (provider: ApiKeyProvider) => file(`llm-${provider}-api-key.enc.json`);

  function read<T>(value: unknown, schema: z.ZodType<T>, missing: T): T {
    if (value === null) return missing;
    const parsed = schema.safeParse(value);
    if (!parsed.success) throw new CredentialStorageError('invalid_data');
    return parsed.data;
  }

  function credentialSchema(provider: ApiKeyProvider | 'tavily' | 'composio') {
    return z.object({ provider: z.literal(provider), api_key: z.string().min(1) }).strict();
  }

  function readApiKey(provider: ApiKeyProvider): string | null {
    const stored = read(apiFile(provider).read(), credentialSchema(provider).nullable(), null);
    return stored?.api_key ?? null;
  }

  function readWebSearchKey(): string | null {
    const stored = read(webSearchFile.read(), credentialSchema('tavily').nullable(), null);
    return stored?.api_key ?? null;
  }

  function readIntegrationKey(): string | null {
    const stored = read(integrationFile.read(), credentialSchema('composio').nullable(), null);
    return stored?.api_key ?? null;
  }

  function readPreferences(): ConnectionPreferences {
    return read(preferencesFile.read(), ConnectionPreferencesSchema, {
      ...EMPTY_CONNECTION_PREFERENCES,
    });
  }

  return {
    readPreferences,
    savePreferences: (preferences: ConnectionPreferences): void =>
      preferencesFile.write(preferences),
    readApiKey,
    saveApiKey: (provider: ApiKeyProvider, apiKey: string): void =>
      apiFile(provider).write({ provider, api_key: apiKey }),
    removeApiKey: (provider: ApiKeyProvider): void => apiFile(provider).remove(),
    readWebSearchKey,
    saveWebSearchKey: (apiKey: string): void =>
      webSearchFile.write({ provider: 'tavily', api_key: apiKey }),
    removeWebSearchKey: (): void => webSearchFile.remove(),
    readIntegrationKey,
    saveIntegrationKey: (apiKey: string): void =>
      integrationFile.write({ provider: 'composio', api_key: apiKey }),
    removeIntegrationKey: (): void => integrationFile.remove(),
  };
}

export type ConnectionSettingsStore = ReturnType<typeof createConnectionSettingsStore>;
