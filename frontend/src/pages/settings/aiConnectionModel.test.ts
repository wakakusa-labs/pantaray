import { expect, it } from 'vitest';

import { CHATGPT_MODEL_CANDIDATES, modelForTarget } from './aiConnectionModel';

it('starts a ChatGPT connection on a model that backend actually serves', () => {
  // 空のままだと経路が unconfigured になり、公開 API の別名は ChatGPT backend に無い。
  for (const model of ['', 'gpt-5.6', 'claude-opus-5-5']) {
    expect(modelForTarget({ method: 'chatgpt', provider: 'openai', model })).toBe(
      CHATGPT_MODEL_CANDIDATES[0]
    );
  }
});

it('keeps a model the target actually offers', () => {
  expect(modelForTarget({ method: 'chatgpt', provider: 'openai', model: 'gpt-5.6-sol' })).toBe(
    'gpt-5.6-sol'
  );
  expect(modelForTarget({ method: 'api_key', provider: 'openai', model: 'gpt-6-luna' })).toBe(
    'gpt-6-luna'
  );
});

it('does not carry a model into a provider that never serves it', () => {
  expect(modelForTarget({ method: 'api_key', provider: 'anthropic', model: 'gpt-6-luna' })).toBe(
    'claude-opus-5-5'
  );
});

it('leaves the typed name alone where there is nothing to choose from', () => {
  expect(
    modelForTarget({ method: 'api_key', provider: 'fireworks', model: 'accounts/x/models/y' })
  ).toBe('accounts/x/models/y');
});
