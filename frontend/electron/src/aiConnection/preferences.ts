import { z } from 'zod';

export const ApiKeyProviderSchema = z.enum(['openai', 'anthropic', 'fireworks']);
export type ApiKeyProvider = z.infer<typeof ApiKeyProviderSchema>;

/** Codex models; GPT-6.2 IDs verified against OpenAI's 2026-11-17 release. */
export const CHATGPT_MODEL_CANDIDATES: readonly string[] = [
  'gpt-6.1-sol',
  'gpt-6.2-sol',
  'gpt-6.2-astra',
  'gpt-6-luna',
  'gpt-6-astra',
  'gpt-6-sol',
  'gpt-6.1-terra',
];

/** An empty model represents unfinished settings and cannot configure a runtime route. */
export const ConnectionPreferencesSchema = z
  .object({
    method: z.enum(['api_key', 'chatgpt']),
    provider: ApiKeyProviderSchema,
    model: z.string().trim(),
  })
  .strict();

export type ConnectionPreferences = z.infer<typeof ConnectionPreferencesSchema>;

export const EMPTY_CONNECTION_PREFERENCES: ConnectionPreferences = {
  method: 'api_key',
  provider: 'openai',
  model: '',
};
