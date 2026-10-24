import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { useI18n } from '@/context/useI18n';
import type { ConnectionStateResult } from '../../electron/src/ipc/schemas/aiConnection';
import type { IntegrationsStatus } from '../../electron/src/settings/integrationSettingsFetch';
import { AppsStep } from './AppsStep';

const auth = {
  runtimeState: { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
} as AuthContextType;

const CONFIGURED: IntegrationsStatus = {
  state: 'configured',
  toolkits: [{ toolkit: 'slack', display_name: 'Slack', logo_url: null, accounts: [] }],
};

function stubBridge(keySaved: boolean) {
  const state: ConnectionStateResult = {
    ok: true,
    settings: {
      preferences: { method: 'api_key', provider: 'openai', model: '' },
      hasSavedApiKey: false,
      hasSavedWebSearchKey: false,
      hasSavedIntegrationKey: keySaved,
      canStoreSecrets: true,
      chatgpt: { status: 'disconnected' },
    },
    runtime: { ok: false, error: 'runtime_unavailable' },
  };
  window.electron = {
    aiConnection: {
      getState: vi.fn(async () => state),
      update: vi.fn(async () => ({ ok: true })),
      onChanged: vi.fn(() => () => undefined),
    },
    integrations: {
      getStatus: vi.fn(async () => (keySaved ? CONFIGURED : { state: 'not_configured' })),
    },
  } as unknown as Window['electron'];
}

function Step({ hasSavedKey }: { hasSavedKey: boolean }) {
  const { t } = useI18n();
  return <AppsStep t={t} hasSavedKey={hasSavedKey} />;
}

function renderStep(keySaved: boolean) {
  stubBridge(keySaved);
  render(
    <AuthContext.Provider value={auth}>
      <UiLanguageProvider initialLanguage="ja">
        <Step hasSavedKey={keySaved} />
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('AppsStep', () => {
  it('without a key, guides to a project key and refuses an account key as it is typed', async () => {
    renderStep(false);

    const input = await screen.findByLabelText('Composio API キー');
    expect(screen.getByText('Composio に登録する')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /ダッシュボードを開く/ })).toHaveAttribute(
      'href',
      'https://dashboard.composio.dev'
    );
    expect(screen.getByText('Session tool execution')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '外部サービスを連携' })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'ck_account' } });
    expect(screen.getByRole('alert')).toHaveTextContent('これはアカウントのキーです。');
    expect(screen.getByRole('button', { name: 'Composio API キー: 保存' })).toBeDisabled();
  });

  it('with a key saved, shows the services instead of the guide', async () => {
    renderStep(true);

    expect(await screen.findByRole('button', { name: '外部サービスを連携' })).toBeInTheDocument();
    expect(screen.getByText(/つなぐサービスを選んでください。/)).toBeInTheDocument();
    expect(screen.queryByText('Composio に登録する')).not.toBeInTheDocument();
  });
});
