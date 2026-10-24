import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import { AiConnectionSection, type AiConnectionActions } from './AiConnectionSection';
import type { AiConnectionState } from '../aiConnectionModel';
import type { Translate } from '../types';

const state: AiConnectionState = {
  runtime: {
    ok: true,
    status: {
      helperInstanceId: 'helper',
      activeOwnerId: 'guest',
      configured: true,
      llmRoute: 'unconfigured',
      webSearchRoute: 'unconfigured',
      cloudSessionState: 'absent',
    },
  },
  method: 'api_key',
  model: '',
  chatgpt: null,
  apiKey: { provider: 'openai', hasSavedKey: false },
  webSearchHasSavedKey: false,
  canStoreSecrets: true,
};

it('hides Pantaray Cloud while keeping ChatGPT and API-key connections', () => {
  const action = vi.fn();
  const actions: AiConnectionActions = {
    selectMethod: action,
    selectProvider: action,
    saveModel: action,
    saveApiKey: action,
    clearApiKey: action,
    signInToChatgpt: action,
    cancelChatgptSignIn: action,
    disconnectChatgpt: action,
    saveWebSearchKey: action,
    clearWebSearchKey: action,
  };
  render(
    <AiConnectionSection
      part="all"
      state={state}
      actions={actions}
      t={((key: string) => key) as Translate}
      pendingOperation={null}
      feedback={null}
    />
  );

  fireEvent.click(screen.getByRole('button', { name: /^settings\.aiConnection\.methodTitle: / }));
  expect(
    screen.queryByRole('menuitemradio', { name: 'settings.aiConnection.method.cloud.title' })
  ).toBeNull();
  expect(
    screen.getByRole('menuitemradio', { name: 'settings.aiConnection.method.chatgpt.title' })
  ).toBeVisible();
  expect(
    screen.getByRole('menuitemradio', { name: 'settings.aiConnection.method.api_key.title' })
  ).toBeVisible();
  expect(screen.queryByText('settings.aiConnection.method.cloud.requiresLogin')).toBeNull();
});
