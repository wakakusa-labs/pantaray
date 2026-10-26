import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type {
  ConnectionCommand,
  ConnectionStateResult,
  ConnectionUpdateResult,
} from '../../electron/src/ipc/schemas/aiConnection';
import type { OnboardingState } from '../../electron/src/ipc/schemas/onboarding';
import { OnboardingGate } from './OnboardingGate';

/** Main's connection store as far as the web search step reads and changes it. */
function stubBridge() {
  let webSearchKey = false;
  const refusals = new Map<ConnectionCommand['operation'], ConnectionUpdateResult>();
  const update = vi.fn(async (command: ConnectionCommand): Promise<ConnectionUpdateResult> => {
    const refused = refusals.get(command.operation);
    if (refused) return refused;
    if (command.operation !== 'save_web_search_key') throw new Error(command.operation);
    webSearchKey = true;
    return { ok: true };
  });
  const getState = vi.fn(
    async (): Promise<ConnectionStateResult> => ({
      ok: true,
      settings: {
        preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6.1-sol' },
        hasSavedApiKey: false,
        hasSavedWebSearchKey: webSearchKey,
        hasSavedIntegrationKey: false,
        canStoreSecrets: true,
        chatgpt: { status: 'connected' },
      },
      runtime: { ok: false, error: 'runtime_unavailable' },
    })
  );

  window.electron = {
    onboarding: {
      getState: async (): Promise<OnboardingState> => ({
        status: 'in_progress',
        step: 'web_search',
      }),
      setState: vi.fn(async (state: OnboardingState) => state),
    },
    aiConnection: { getState, update, onChanged: () => () => undefined },
    screenshot: {
      getStatus: async () => true,
      onStatusChanged: () => () => undefined,
      getPermissions: async () => [],
    },
    integrations: { getStatus: async () => ({ state: 'not_configured' }) },
  } as unknown as Window['electron'];

  return {
    update,
    getState,
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

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('WebSearchStep', () => {
  it('saves the Tavily key, then shows it as saved with a way to change it', async () => {
    const bridge = stubBridge();
    renderSetup();
    const link = await screen.findByRole('link', { name: /Tavily でキーを作る/ });
    expect(link).toHaveAttribute('target', '_blank');
    expect(next()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'スキップ' })).toBeEnabled();

    fireEvent.change(screen.getByLabelText('Tavily の API キー'), {
      target: { value: 'tvly-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await row('Tavily の API キー').findByText('保存済み')).toBeVisible();
    expect(bridge.update).toHaveBeenCalledWith({
      operation: 'save_web_search_key',
      apiKey: 'tvly-1',
    });
    expect(next()).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'スキップ' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Tavily の API キー: 変更' }));
    expect(screen.getByLabelText('Tavily の API キー')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'キャンセル' }));
    expect(row('Tavily の API キー').getByText('保存済み')).toBeVisible();
  });

  it('says why a key was not saved and keeps it typed', async () => {
    const bridge = stubBridge();
    bridge.refuse('save_web_search_key', { ok: false, error: 'write_failed' });
    renderSetup();
    const key = await screen.findByLabelText('Tavily の API キー');
    fireEvent.change(key, { target: { value: 'tvly-1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(await row('Tavily の API キー').findByRole('alert')).toBeVisible();
    expect(key).toHaveValue('tvly-1');
    expect(next()).toBeDisabled();
  });

  it('says when the connection cannot be read, and shows the step after a retry', async () => {
    const bridge = stubBridge();
    bridge.getState.mockResolvedValueOnce({ ok: false, error: 'read_failed' });
    renderSetup();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '保存済みの資格情報を読み取れませんでした'
    );
    expect(screen.queryByLabelText('Tavily の API キー')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '設定を再取得' }));
    expect(await screen.findByLabelText('Tavily の API キー')).toBeEnabled();
  });
});
