import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ConnectionStateResult } from '../../electron/src/ipc/schemas/aiConnection';
import type { OnboardingState } from '../../electron/src/ipc/schemas/onboarding';
import { OnboardingGate } from './OnboardingGate';

const auth = {
  runtimeState: { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
} as AuthContextType;

type Setup = { llmRoute: 'unconfigured' | 'direct'; recording: boolean };

function stubBridge(getState: () => Promise<OnboardingState>, setup: Setup) {
  const listeners = new Set<() => void>();
  const connection = (): ConnectionStateResult => ({
    ok: true,
    settings: {
      preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6.1-sol' },
      hasSavedApiKey: false,
      hasSavedWebSearchKey: false,
      hasSavedIntegrationKey: false,
      canStoreSecrets: true,
      chatgpt: { status: setup.llmRoute === 'direct' ? 'connected' : 'disconnected' },
    },
    runtime: {
      ok: true,
      status: {
        helperInstanceId: 'helper',
        activeOwnerId: 'guest',
        configured: true,
        cloudSessionState: 'absent',
        llmRoute: setup.llmRoute,
        webSearchRoute: 'unconfigured',
      },
    },
  });
  const setState = vi.fn(async (state: OnboardingState) => state);
  window.electron = {
    onboarding: { getState: vi.fn(getState), setState },
    aiConnection: {
      getState: vi.fn(async () => connection()),
      update: vi.fn(async () => ({ ok: true })),
      onChanged: vi.fn((listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      }),
    },
    screenshot: {
      getStatus: vi.fn(async () => setup.recording),
      onStatusChanged: vi.fn(() => () => undefined),
      getGateState: vi.fn(async () => ({
        permissions: { accessibility: false, screenRecording: false },
        introRequested: false,
      })),
    },
    integrations: { getStatus: vi.fn(async () => ({ state: 'not_configured' })) },
  } as unknown as Window['electron'];
  return {
    setState,
    // What main does after a Settings section saves: every reader reads the connection again.
    connect: async () => {
      setup.llmRoute = 'direct';
      await act(async () => listeners.forEach((listener) => listener()));
    },
  };
}

function renderGate() {
  render(
    <AuthContext.Provider value={auth}>
      <UiLanguageProvider initialLanguage="ja">
        <OnboardingGate>
          <h1>App</h1>
        </OnboardingGate>
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}

const steps = () => within(screen.getByRole('navigation', { name: 'はじめの準備' }));
const next = () => screen.getByRole('button', { name: '次へ' });

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('OnboardingGate', () => {
  it('draws nothing until the state is read, so a finished setup never flashes', async () => {
    let resolve: (state: OnboardingState) => void = () => undefined;
    stubBridge(() => new Promise((r) => (resolve = r)), { llmRoute: 'direct', recording: true });
    renderGate();
    expect(screen.getByText('読み込み中…')).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();

    await act(async () => resolve({ status: 'completed' }));
    expect(screen.getByRole('heading', { name: 'App' })).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('holds a required step until it is done, then saves each step it enters', async () => {
    const bridge = stubBridge(async () => ({ status: 'in_progress', step: 'ai' }), {
      llmRoute: 'unconfigured',
      recording: false,
    });
    renderGate();
    await screen.findByRole('heading', { name: /AI をつなぐ/ });
    expect(next()).toBeDisabled();
    expect(screen.getByText('AI をつなぐと次へ進めます')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'スキップ' })).not.toBeInTheDocument();
    expect(steps().getByRole('button', { name: /Web 検索/ })).toBeDisabled();

    await bridge.connect();
    await waitFor(() => expect(next()).toBeEnabled());
    fireEvent.click(next());
    expect(bridge.setState).toHaveBeenLastCalledWith({ status: 'in_progress', step: 'recording' });
    expect(screen.getByRole('heading', { name: /記録を始める/ })).toHaveFocus();
    expect(steps().getByRole('button', { name: /AI をつなぐ/ })).toHaveTextContent('済み');
    expect(next()).toBeDisabled();
    expect(screen.getByText('記録をオンにすると次へ進めます')).toBeInTheDocument();
  });

  it('resumes at the saved step and reads the optional steps it went past as skipped', async () => {
    const bridge = stubBridge(async () => ({ status: 'in_progress', step: 'apps' }), {
      llmRoute: 'direct',
      recording: true,
    });
    renderGate();
    await screen.findByRole('heading', { name: /アプリをつなぐ/ });
    await waitFor(() =>
      expect(steps().getByRole('button', { name: /記録を始める/ })).toHaveTextContent('済み')
    );
    expect(steps().getByRole('button', { name: /Web 検索/ })).toHaveTextContent('スキップ');

    fireEvent.click(screen.getByRole('button', { name: 'スキップ' }));
    expect(bridge.setState).toHaveBeenLastCalledWith({ status: 'in_progress', step: 'done' });
    expect(screen.getByRole('heading', { name: '準備ができました' })).toBeInTheDocument();
    expect(steps().getByRole('button', { name: /アプリをつなぐ/ })).toHaveTextContent('スキップ');
  });

  it('leaves only once completed is saved, and keeps the user on a failed save', async () => {
    const bridge = stubBridge(async () => ({ status: 'in_progress', step: 'done' }), {
      llmRoute: 'direct',
      recording: true,
    });
    bridge.setState.mockRejectedValueOnce(new Error('disk full'));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    renderGate();
    const start = await screen.findByRole('button', { name: '使い始める' });

    fireEvent.click(start);
    expect(await screen.findByRole('alert')).toHaveTextContent('保存できませんでした');
    expect(screen.queryByRole('heading', { name: 'App' })).not.toBeInTheDocument();
    expect(start).toBeEnabled();

    fireEvent.click(start);
    expect(await screen.findByRole('heading', { name: 'App' })).toBeInTheDocument();
    expect(bridge.setState).toHaveBeenLastCalledWith({ status: 'completed' });
  });
});
