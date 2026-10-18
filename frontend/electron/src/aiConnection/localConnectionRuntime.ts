import { createChatgptLogin, type ChatgptLogin } from '../auth/chatgptLogin';
import { createChatgptTokenStore } from '../auth/chatgptTokenStore';
import { createLocalBackendAuthContextController } from '../auth/localBackendAuthContextController';
import { CredentialStorageError, type CredentialEncryption } from '../auth/encryptedJsonFile';
import { createConnectionSettingsStore } from './settingsStore';
import type { ApiKeyProvider, ConnectionPreferences } from './preferences';
import type { ConnectionConfiguration, LlmConnection } from './runtimeConnection';

type AuthControllerParams = Parameters<typeof createLocalBackendAuthContextController>[0];

type SettingsStoreTarget = 'chatgpt' | 'preferences' | 'tavily' | 'composio' | ApiKeyProvider;

/** Identifies only the failed store, never its path or contents. */
export class ConnectionSettingsReadError extends CredentialStorageError {
  constructor(
    code: Extract<CredentialStorageError['code'], 'invalid_data' | 'read_failed'>,
    readonly target: SettingsStoreTarget
  ) {
    super(code);
  }
}

/** Main owns saved settings and OAuth; the existing auth queue owns every helper update. */
export function createLocalConnectionRuntime(params: {
  userDataDir: string;
  safeStorage: CredentialEncryption;
  whenReady: () => Promise<void>;
  onChanged: () => void;
  openExternal: (url: string) => Promise<void>;
  sessionSync: AuthControllerParams['sessionSync'];
  helperManager: AuthControllerParams['helperManager'];
  logger: AuthControllerParams['logger'];
  onRuntimeUnavailable: () => void;
}) {
  const store = createConnectionSettingsStore(params);
  const chatgptStore = createChatgptTokenStore(params);
  let login: ChatgptLogin | null = null;
  let stateSync: Promise<void> = Promise.resolve();

  function notifyChanged(): void {
    try {
      params.onChanged();
    } catch {
      // A closing renderer cannot change the outcome of a persisted operation.
      params.logger?.error?.('LOCAL_CONNECTION_NOTIFICATION_FAILED');
    }
  }

  async function initialize(): Promise<ChatgptLogin> {
    await params.whenReady();
    if (!login) {
      login = createChatgptLogin({
        store: chatgptStore,
        openExternal: params.openExternal,
        onStateChanged: () => {
          stateSync = authContext.updateConnections(() => {}).finally(notifyChanged);
          void stateSync.catch(() => params.logger?.error?.('LOCAL_CONNECTION_SYNC_FAILED'));
        },
      });
    }
    return login;
  }

  async function getConfiguration(): Promise<ConnectionConfiguration> {
    const chatgpt = (await initialize()).getState();
    const preferences = store.readPreferences();
    const { method, model, provider } = preferences;
    let llmConnection: LlmConnection | null = null;
    if (model) {
      if (method === 'chatgpt') {
        if (chatgpt.status === 'connected') {
          llmConnection = { kind: 'chatgpt', model, credential: chatgpt.credential };
        }
      } else {
        const apiKey = store.readApiKey(provider);
        if (apiKey) {
          llmConnection = { kind: 'api_key', provider, model, api_key: apiKey };
        }
      }
    }
    const searchKey = store.readWebSearchKey();
    const integrationKey = store.readIntegrationKey();
    return {
      llmConnection,
      webSearchCredential: searchKey ? { provider: 'tavily', api_key: searchKey } : null,
      integrationCredential: integrationKey
        ? { provider: 'composio', api_key: integrationKey }
        : null,
    };
  }

  const authContext = createLocalBackendAuthContextController({
    sessionSync: params.sessionSync,
    helperManager: params.helperManager,
    getConnections: getConfiguration,
    onRuntimeUnavailable: params.onRuntimeUnavailable,
    logger: params.logger,
  });

  async function mutate(change: () => void): Promise<void> {
    await params.whenReady();
    try {
      await authContext.updateConnections(change);
    } finally {
      notifyChanged();
    }
  }

  return {
    authContext,
    getConfiguration,
    getStatus: params.sessionSync.getConnectionStatus,
    async getSettings() {
      let target: SettingsStoreTarget = 'chatgpt';
      try {
        const state = (await initialize()).getState();
        target = 'preferences';
        const preferences = store.readPreferences();
        target = preferences.provider;
        const hasSavedApiKey = store.readApiKey(preferences.provider) !== null;
        target = 'tavily';
        const hasSavedWebSearchKey = store.readWebSearchKey() !== null;
        target = 'composio';
        const hasSavedIntegrationKey = store.readIntegrationKey() !== null;
        return {
          preferences,
          hasSavedApiKey,
          hasSavedWebSearchKey,
          hasSavedIntegrationKey,
          canStoreSecrets: params.safeStorage.isEncryptionAvailable(),
          chatgpt: { status: state.status },
        };
      } catch (error) {
        if (
          error instanceof CredentialStorageError &&
          (error.code === 'invalid_data' || error.code === 'read_failed')
        ) {
          throw new ConnectionSettingsReadError(error.code, target);
        }
        throw error;
      }
    },
    savePreferences: (preferences: ConnectionPreferences) =>
      mutate(() => store.savePreferences(preferences)),
    saveApiKey: (provider: ApiKeyProvider, key: string) =>
      mutate(() => store.saveApiKey(provider, key)),
    removeApiKey: (provider: ApiKeyProvider) => mutate(() => store.removeApiKey(provider)),
    saveWebSearchKey: (key: string) => mutate(() => store.saveWebSearchKey(key)),
    removeWebSearchKey: () => mutate(store.removeWebSearchKey),
    saveIntegrationKey: (key: string) => mutate(() => store.saveIntegrationKey(key)),
    removeIntegrationKey: () => mutate(store.removeIntegrationKey),
    async signInToChatgpt() {
      const result = await (await initialize()).signIn();
      if (!result.ok) return { ok: false as const, error: result.error };
      await stateSync;
      return { ok: true as const };
    },
    cancelChatgptSignIn: () => login?.cancelSignIn(),
    async disconnectChatgpt() {
      await params.whenReady();
      const currentLogin = login;
      // Without a loaded login, delete synchronously before another reader can load old tokens.
      const result = currentLogin ? currentLogin.disconnect() : chatgptStore.clear();
      if (!result.ok) return result;
      if (currentLogin) await stateSync;
      else await mutate(() => {});
      return { ok: true as const };
    },
    dispose: () => login?.dispose(),
  };
}

export type LocalConnectionRuntime = ReturnType<typeof createLocalConnectionRuntime>;
