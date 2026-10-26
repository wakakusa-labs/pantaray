import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type {
  ConnectionCommand,
  ConnectionUpdateResult,
} from '../../electron/src/ipc/schemas/aiConnection';
import type { OnboardingState } from '../../electron/src/ipc/schemas/onboarding';
import { OnboardingGate } from './OnboardingGate';

type Provider = 'openai' | 'anthropic' | 'fireworks';

/** Main's connection store: what each command saves, and the route those saves make. */
function stubBridge(initial: { chatgptConnected?: boolean } = {}) {
  const store = {
    preferences: { method: 'api_key', provider: 'openai', model: '' } as {
      method: 'api_key' | 'chatgpt';
      provider: Provider;
      model: string;
    },
    chatgpt: initial.chatgptConnected ? 'connected' : 'disconnected',
    apiKeys: new Set<Provider>(),
  };
  if (initial.chatgptConnected) {
    store.preferences = { method: 'chatgpt', provider: 'openai', model: 'gpt-6.1-sol' };
  }
  const routed = () =>
    store.preferences.model !== '' &&
    (store.preferences.method === 'chatgpt'
      ? store.chatgpt === 'connected'
      : store.apiKeys.has(store.preferences.provider));
  let signIn: ((result: ConnectionUpdateResult) => void) | null = null;
  const refusals = new Map<ConnectionCommand['operation'], ConnectionUpdateResult>();

  const update = vi.fn(async (command: ConnectionCommand): Promise<ConnectionUpdateResult> => {
    const refused = refusals.get(command.operation);
    if (refused) return refused;
    switch (command.operation) {
      case 'sign_in_chatgpt':
        return new Promise((resolve) => {
          signIn = (result) => {
            if (result.ok) store.chatgpt = 'connected';
            resolve(result);
          };
        });
      case 'cancel_chatgpt_sign_in':
        signIn?.({ ok: false, error: 'cancelled' });
        return { ok: true };
      case 'save_preferences':
        store.preferences = command.preferences;
        return { ok: true };
      case 'save_api_key':
        store.apiKeys.add(command.provider);
        return { ok: true };
      default:
        throw new Error(`unexpected ${command.operation}`);
    }
  });

  window.electron = {
    onboarding: {
      getState: async (): Promise<OnboardingState> => ({ status: 'in_progress', step: 'ai' }),
      setState: vi.fn(async (state: OnboardingState) => state),
    },
    aiConnection: {
      getState: async () => ({
        ok: true,
        settings: {
          preferences: { ...store.preferences },
          hasSavedApiKey: store.apiKeys.has(store.preferences.provider),
          hasSavedWebSearchKey: false,
          hasSavedIntegrationKey: false,
          canStoreSecrets: true,
          chatgpt: { status: store.chatgpt },
        },
        runtime: {
          ok: true,
          status: {
            llmRoute: routed() ? 'direct' : 'unconfigured',
            webSearchRoute: 'unconfigured',
          },
        },
      }),
      update,
      onChanged: () => () => undefined,
    },
    screenshot: {
      getStatus: async () => true,
      onStatusChanged: () => () => undefined,
      getPermissions: async () => [],
    },
    integrations: { getStatus: async () => ({ state: 'not_configured' }) },
  } as unknown as Window['electron'];

  return {
    update,
    operations: () => update.mock.calls.map(([command]) => command.operation),
    finishSignIn: async (result: ConnectionUpdateResult = { ok: true }) => {
      await act(async () => signIn?.(result));
    },
    refuse: (operation: ConnectionCommand['operation'], result: ConnectionUpdateResult) =>
      refusals.set(operation, result),
  };
}

function renderSetup() {
  render(
    <AuthContext.Provider
      value={
        {
          runtimeState: { status: 'ready', owner: { kind: 'guest', id: 'guest' } },
        } as AuthContextType
      }
    >
      <UiLanguageProvider initialLanguage="ja">
        <OnboardingGate>
          <h1>App</h1>
        </OnboardingGate>
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}

// The step's checklist, not the steps panel that shares its titles.
const row = (title: string) =>
  within(
    within(document.querySelector<HTMLElement>('ul.checklist')!).getByText(title).closest('li')!
  );
const next = () => screen.getByRole('button', { name: '次へ' });
const model = () => screen.getByRole('combobox', { name: 'モデル' });

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('AiStep', () => {
  it('signs in with ChatGPT, then saves the method with a ChatGPT model', async () => {
    const bridge = stubBridge();
    renderSetup();
    fireEvent.click(await screen.findByRole('button', { name: 'ChatGPT でログイン' }));
    expect(model()).toBeDisabled();
    expect(next()).toBeDisabled();

    expect(await screen.findByText('ブラウザでログインしてください')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'ChatGPT でログイン' })).not.toBeInTheDocument();
    await bridge.finishSignIn();

    expect(await row('AI をつなぐ').findByText('接続済み')).toBeVisible();
    expect(row('AI をつなぐ').getByText('ChatGPT アカウント')).toBeVisible();
    // Saving the method before the sign-in ends would drop a working connection.
    expect(bridge.operations()).toEqual(['sign_in_chatgpt', 'save_preferences']);
    expect(bridge.update).toHaveBeenLastCalledWith({
      operation: 'save_preferences',
      preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6.1-sol' },
    });
    expect(model()).toBeEnabled();
    expect(model()).toHaveValue('gpt-6.1-sol');
    expect(next()).toBeEnabled();

    fireEvent.change(model(), { target: { value: 'gpt-6-luna' } });
    await waitFor(() => expect(model()).toHaveValue('gpt-6-luna'));
    expect(bridge.update).toHaveBeenLastCalledWith({
      operation: 'save_preferences',
      preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6-luna' },
    });
  });

  it('treats a cancelled sign-in as no connection and no error', async () => {
    const bridge = stubBridge();
    renderSetup();
    fireEvent.click(await screen.findByRole('button', { name: 'ChatGPT でログイン' }));
    fireEvent.click(await screen.findByRole('button', { name: 'キャンセル' }));

    expect(await screen.findByRole('button', { name: 'ChatGPT でログイン' })).toBeEnabled();
    expect(bridge.operations()).toEqual(['sign_in_chatgpt', 'cancel_chatgpt_sign_in']);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(next()).toBeDisabled();
  });

  it('connects with an API key for the chosen provider and its default model', async () => {
    const bridge = stubBridge();
    renderSetup();
    fireEvent.click(await screen.findByRole('button', { name: 'API キーを使う' }));
    fireEvent.change(screen.getByRole('combobox', { name: 'プロバイダー' }), {
      target: { value: 'anthropic' },
    });
    expect(model()).toBeDisabled();
    expect(model()).toHaveValue('claude-sonnet-5-5');
    fireEvent.change(screen.getByLabelText('Anthropic の API キー'), {
      target: { value: ' sk-ant ' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await row('AI をつなぐ').findByText('Anthropic の API キー')).toBeVisible();
    expect(bridge.update.mock.calls.map(([command]) => command)).toEqual([
      { operation: 'save_api_key', provider: 'anthropic', apiKey: 'sk-ant' },
      {
        operation: 'save_preferences',
        preferences: { method: 'api_key', provider: 'anthropic', model: 'claude-sonnet-5-5' },
      },
    ]);
    expect(model()).toBeEnabled();
    expect(next()).toBeEnabled();
  });

  it('keeps the typed key and says why when the key is not saved', async () => {
    const bridge = stubBridge();
    bridge.refuse('save_api_key', { ok: false, error: 'write_failed' });
    renderSetup();
    fireEvent.click(await screen.findByRole('button', { name: 'API キーを使う' }));
    const key = screen.getByLabelText('OpenAI の API キー');
    fireEvent.change(key, { target: { value: 'sk-1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await row('AI をつなぐ').findByRole('alert')).toBeVisible();
    expect(key).toHaveValue('sk-1');
    expect(bridge.operations()).toEqual(['save_api_key']);
    expect(next()).toBeDisabled();
  });

  it('opens the choices again on Change without disconnecting', async () => {
    const bridge = stubBridge({ chatgptConnected: true });
    renderSetup();
    fireEvent.click(await screen.findByRole('button', { name: 'AI をつなぐ: 変更' }));

    expect(screen.getByRole('button', { name: 'ChatGPT でログイン' })).toBeEnabled();
    expect(model()).toBeDisabled();
    expect(next()).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'キャンセル' }));
    expect(row('AI をつなぐ').getByText('接続済み')).toBeVisible();
    expect(bridge.update).not.toHaveBeenCalled();
  });
});
