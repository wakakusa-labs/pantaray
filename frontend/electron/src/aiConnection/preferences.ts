import { z } from 'zod';

export const ApiKeyProviderSchema = z.enum(['openai', 'anthropic', 'fireworks']);
export type ApiKeyProvider = z.infer<typeof ApiKeyProviderSchema>;

/** Visible Codex models, verified 2026-09-30 against openai/codex models-manager/models.json. */
export const CHATGPT_MODEL_CANDIDATES: readonly string[] = [
  'gpt-6-luna',
  'gpt-6-astra',
  'gpt-6.1-sol',
  'gpt-6-sol',
  'gpt-5.6-sol',
  'gpt-5.6-terra',
  'gpt-5.6-luna',
  'gpt-5.5',
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
