import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type {
  ConnectionCommand,
  ConnectionUpdateResult,
} from '../../electron/src/ipc/schemas/aiConnection';
import type { OnboardingState } from '../../electron/src/ipc/schemas/onboarding';
import type {
  IntegrationAccountStatus,
  IntegrationsStatus,
} from '../../electron/src/settings/integrationSettingsFetch';
import { OnboardingGate } from './OnboardingGate';

const OWNER = { kind: 'guest', id: 'guest' } as const;

const ACCOUNT: IntegrationAccountStatus = {
  account: 'acct-slack-1',
  label: 'you@example.com',
  health: 'ok',
  access: 'read_only',
  // The setup steps do not offer access choices.
  options: [],
};

const services = (connected: boolean, health = ACCOUNT.health): IntegrationsStatus => ({
  state: 'configured',
  toolkits: [
    {
      toolkit: 'slack',
      display_name: 'Slack',
      logo_url: null,
      accounts: connected ? [{ ...ACCOUNT, health }] : [],
    },
    { toolkit: 'gmail', display_name: 'Gmail', logo_url: null, accounts: [] },
  ],
});

/** Main's stores as far as the apps and done steps read and change them. */
function stubBridge(
  step: 'apps' | 'done',
  initial: { keySaved?: boolean; status?: IntegrationsStatus } = {}
) {
  let keySaved = initial.keySaved ?? false;
  let status = initial.status ?? services(false);
  const update = vi.fn(async (command: ConnectionCommand): Promise<ConnectionUpdateResult> => {
    if (command.operation !== 'save_integration_key') throw new Error(command.operation);
    keySaved = true;
    return { ok: true };
  });
  const integrations = {
    getStatus: vi.fn(async () => (keySaved ? status : { state: 'not_configured' })),
    connect: vi.fn(async () => ({
      kind: 'started',
      connection_id: 'conn-1',
      redirect_url: 'https://connect.example/slack',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    })),
    getConnectionState: vi.fn(async () => 'active'),
  };
  window.electron = {
    onboarding: {
      getState: async (): Promise<OnboardingState> => ({ status: 'in_progress', step }),
      setState: vi.fn(async (state: OnboardingState) => state),
    },
    aiConnection: {
      getState: async () => ({
        ok: true,
        settings: {
          preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6.1-sol' },
          hasSavedApiKey: false,
          hasSavedWebSearchKey: false,
          hasSavedIntegrationKey: keySaved,
          canStoreSecrets: true,
          chatgpt: { status: 'connected' },
        },
        runtime: { ok: true, status: { llmRoute: 'direct', webSearchRoute: 'unconfigured' } },
      }),
      update,
      onChanged: () => () => undefined,
    },
    screenshot: {
      getStatus: async () => true,
      onStatusChanged: () => () => undefined,
      getPermissions: async () => [],
    },
    integrations,
  } as unknown as Window['electron'];
  return {
    update,
    integrations,
    setStatus: (next: IntegrationsStatus) => (status = next),
  };
}

async function renderSetup() {
  render(
    <AuthContext.Provider
      value={{ runtimeState: { status: 'ready', message: null, owner: OWNER } } as AuthContextType}
    >
      <UiLanguageProvider initialLanguage="ja">
        <OnboardingGate>
          <h1>App</h1>
        </OnboardingGate>
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
  // The step draws once the connection is read.
  await waitFor(() => expect(document.querySelector('ul.checklist')).not.toBeNull());
}

// The step's checklist, not the steps panel that shares its titles.
const item = (title: string) =>
  within(document.querySelector<HTMLElement>('ul.checklist')!).getByText(title).closest('li')!;
const row = (title: string) => within(item(title));
const isChecked = (title: string) => item(title).classList.contains('check-item--done');
const next = () => screen.getByRole('button', { name: '次へ' });
const add = () => row('サービスをつなぐ').getByRole('button', { name: '追加' });

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.electron;
});

describe('AppsStep', () => {
  it('refuses an account key, and opens the services once a saved key lists them', async () => {
    const bridge = stubBridge('apps');
    await renderSetup();
    const key = await screen.findByLabelText('Composio の API キー');
    expect(row('Composio の API キー').getByText('キーの作り方')).toBeInTheDocument();
    expect(add()).toBeDisabled();

    fireEvent.change(key, { target: { value: 'ck_account' } });
    expect(row('Composio の API キー').getByRole('alert')).toHaveTextContent('アカウントのキー');
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();

    fireEvent.change(key, { target: { value: 'ak_project' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await row('Composio の API キー').findByText('保存済み')).toBeVisible();
    expect(bridge.update).toHaveBeenCalledWith({
      operation: 'save_integration_key',
      apiKey: 'ak_project',
    });
    await waitFor(() => expect(isChecked('Composio の API キー')).toBe(true));
    expect(add()).toBeEnabled();
    expect(isChecked('サービスをつなぐ')).toBe(false);
    expect(next()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'スキップ' })).toBeEnabled();
  });

  it('keeps a saved key that the backend refuses unfinished, with the reason', async () => {
    stubBridge('apps', { keySaved: true, status: { state: 'key_rejected' } });
    await renderSetup();

    expect(await row('Composio の API キー').findByRole('alert')).toHaveTextContent(
      'このキーは使えません'
    );
    expect(isChecked('Composio の API キー')).toBe(false);
    expect(row('Composio の API キー').getByText('保存済み')).toBeVisible();
    expect(add()).toBeDisabled();
  });

  it('connects a service from the add dialog, then lists it and allows Next', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
    vi.spyOn(window, 'open').mockReturnValue(null);
    const bridge = stubBridge('apps', { keySaved: true });
    await renderSetup();
    await waitFor(() => expect(add()).toBeEnabled());

    fireEvent.click(add());
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Slack に接続' }));
    expect(
      await row('サービスをつなぐ').findByText(/^Slack: ブラウザで続けてください/)
    ).toBeVisible();
    expect(row('サービスをつなぐ').getByRole('button', { name: 'キャンセル' })).toBeEnabled();

    bridge.setStatus(services(true));
    await act(() => vi.advanceTimersByTimeAsync(2_000));

    await waitFor(() => expect(isChecked('サービスをつなぐ')).toBe(true));
    expect(row('サービスをつなぐ').getByText('Slack')).toBeVisible();
    expect(next()).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'スキップ' })).not.toBeInTheDocument();
  });

  it('shows a connect that could not start on the services row', async () => {
    const bridge = stubBridge('apps', { keySaved: true });
    bridge.integrations.connect.mockRejectedValueOnce(new Error('offline'));
    await renderSetup();
    await waitFor(() => expect(add()).toBeEnabled());

    fireEvent.click(add());
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Slack に接続' })
    );

    expect(await row('サービスをつなぐ').findByRole('alert')).toHaveTextContent(
      'Slack: 接続を始められませんでした。'
    );
    expect(isChecked('サービスをつなぐ')).toBe(false);
    expect(add()).toBeEnabled();
  });
  it('reads the services again when the step is shown, so a connect finished meanwhile counts', async () => {
    const bridge = stubBridge('apps', { keySaved: true });
    await renderSetup();
    await waitFor(() => expect(add()).toBeEnabled());
    expect(next()).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '戻る' }));
    await screen.findByRole('heading', { name: /Web 検索/ });
    // The browser step finished after the apps step stopped following it.
    bridge.setStatus(services(true));
    fireEvent.click(screen.getByRole('button', { name: 'スキップ' }));

    await waitFor(() => expect(next()).toBeEnabled());
    expect(isChecked('サービスをつなぐ')).toBe(true);
  });

  it('does not count an account that needs reconnecting, and says so', async () => {
    stubBridge('apps', { keySaved: true, status: services(true, 'reauthorization_required') });
    await renderSetup();

    expect(await row('サービスをつなぐ').findByRole('alert')).toHaveTextContent(
      'つなぎ直しが要ります'
    );
    expect(isChecked('サービスをつなぐ')).toBe(false);
    expect(row('サービスをつなぐ').queryByText('Slack')).not.toBeInTheDocument();
    expect(next()).toBeDisabled();
  });
});

describe('DoneStep', () => {
  it('shows apps as neither done nor skipped until the services are read', async () => {
    const bridge = stubBridge('done', { keySaved: true, status: services(true) });
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const read = bridge.integrations.getStatus.getMockImplementation()!;
    bridge.integrations.getStatus.mockImplementation(() => gate.then(read));
    await renderSetup();

    expect(await row('アプリ').findByText('状態を取得中…')).toBeVisible();
    expect(item('アプリ').classList.contains('check-item--skipped')).toBe(false);
    const appsStep = within(screen.getByRole('navigation')).getByRole('button', {
      name: /アプリをつなぐ/,
    });
    expect(appsStep).not.toHaveTextContent('スキップ');

    await act(async () => release());
    await waitFor(() => expect(isChecked('アプリ')).toBe(true));
    expect(appsStep).toHaveTextContent('済み');
  });

  it('says the services could not be read, and reads them again on request', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const bridge = stubBridge('done', { keySaved: true, status: services(true) });
    const read = bridge.integrations.getStatus.getMockImplementation()!;
    bridge.integrations.getStatus.mockRejectedValue(new Error('offline'));
    await renderSetup();

    expect(await row('アプリ').findByRole('alert')).toHaveTextContent(
      'サービスの一覧を取得できませんでした。'
    );
    expect(item('アプリ').classList.contains('check-item--skipped')).toBe(false);

    bridge.integrations.getStatus.mockImplementation(read);
    fireEvent.click(row('アプリ').getByRole('button', { name: 'もう一度読み込む' }));
    await waitFor(() => expect(isChecked('アプリ')).toBe(true));
  });

  it('marks what was set up and what was skipped, and says skipped items can wait', async () => {
    stubBridge('done', { keySaved: true, status: services(true) });
    await renderSetup();

    expect(await row('アプリ').findByText('Slack')).toBeVisible();
    expect(isChecked('アプリ')).toBe(true);
    expect(row('アプリ').getByText('接続済み')).toBeVisible();
    expect(item('Web 検索').classList.contains('check-item--skipped')).toBe(true);
    expect(row('Web 検索').getByText('スキップ')).toBeVisible();
    expect(isChecked('AI')).toBe(true);
    expect(row('AI').getByText('ChatGPT アカウント')).toBeVisible();
    expect(screen.getByText('スキップした項目は、あとから設定で行えます。')).toBeVisible();
  });
});
