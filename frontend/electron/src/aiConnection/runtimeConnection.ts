import type { ApiKeyProvider } from './preferences';

export type ConnectionRoute = 'cloud' | 'direct' | 'unconfigured';

/** Only these ChatGPT fields cross the privileged control socket; refresh tokens stay in main. */
export type ChatgptCredential = {
  access_token: string;
  expires_at: string;
  account_id: string;
};

export type LlmConnection =
  | { kind: 'chatgpt'; model: string; credential: ChatgptCredential }
  | {
      kind: 'api_key';
      provider: ApiKeyProvider;
      model: string;
      api_key: string;
    };

export type WebSearchCredential = { provider: 'tavily'; api_key: string };

/** The external-app integrations' connection method key; Composio is the only method. */
export type IntegrationCredential = { provider: 'composio'; api_key: string };

export type ConnectionConfiguration = {
  llmConnection: LlmConnection | null;
  webSearchCredential: WebSearchCredential | null;
  integrationCredential: IntegrationCredential | null;
};
