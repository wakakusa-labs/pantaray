import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import type { ConnectionStateResult } from '../../electron/src/ipc/schemas/aiConnection';
import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { AiConnectionNotice } from './AiConnectionNotice';

vi.mock('../../electron/src/auth/accountLoginFeature', () => ({
  PANTARAY_ACCOUNT_LOGIN_ENABLED: true,
}));

type Loaded = Extract<ConnectionStateResult, { ok: true }>;
function connection() {
  return {
    ok: true,
    settings: {
      preferences: { method: 'api_key', provider: 'openai', model: '' },
      hasSavedApiKey: false,
      hasSavedWebSearchKey: false,
      hasSavedIntegrationKey: false,
      canStoreSecrets: true,
      chatgpt: { status: 'disconnected' },
    },
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
  } satisfies Loaded;
}
function bridge() {
  const changes = new Set<() => void>();
  const api = {
    getState: vi.fn<() => Promise<ConnectionStateResult>>().mockResolvedValue(connection()),
    onChanged: (notify: () => void) => {
      changes.add(notify);
      return () => {
        changes.delete(notify);
      };
    },
  };
  vi.stubGlobal('electron', { aiConnection: api });
  return { ...api, notify: () => changes.forEach((notify) => notify()) };
}
function subject(path = '/history', authStatus: AuthContextType['authStatus'] = 'unauthenticated') {
  const auth: AuthContextType = {
    authStatus,
    user: null,
    session: null,
    loading: false,
    runtimeState:
      authStatus === 'expired'
        ? { status: 'degraded', message: 'helper unavailable', owner: null }
        : { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
    signIn: vi.fn(),
    signUp: vi.fn(),
    signOut: vi.fn(),
    resetPassword: vi.fn(),
  };
  return (
    <AuthContext.Provider value={auth}>
      <UiLanguageProvider initialLanguage="ja">
        <MemoryRouter initialEntries={[path]}>
          <AiConnectionNotice />
        </MemoryRouter>
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('waits for a known route and guides an unconfigured user to the AI settings section', async () => {
  const api = bridge();
  let resolve!: (state: ConnectionStateResult) => void;
  api.getState.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    })
  );
  render(subject());
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  await act(async () => resolve(connection()));
  expect(screen.getByRole('link', { name: 'AI接続を設定' })).toHaveAttribute(
    'href',
    '/settings?section=ai_connection'
  );
  const configured = connection();
  api.getState.mockResolvedValue({
    ...configured,
    runtime: {
      ok: true,
      status: { ...configured.runtime.status, llmRoute: 'direct' },
    },
  });
  act(api.notify);
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
});

it.each(['read', 'runtime'] as const)(
  'distinguishes %s failure from an unconfigured connection',
  async (failure) => {
    const api = bridge();
    render(subject());
    await screen.findByText('AIを使うには、接続方法を設定してください。');
    if (failure === 'read') api.getState.mockRejectedValueOnce(new Error('IPC disconnected'));
    else
      api.getState.mockResolvedValue({
        ...connection(),
        runtime: { ok: false, error: 'runtime_unavailable' },
      });
    act(api.notify);
    expect(
      await screen.findByText('AIの接続状態を確認できません。設定画面で確認してください。')
    ).toBeVisible();
    expect(
      screen.queryByText('AIを使うには、接続方法を設定してください。')
    ).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'AI接続を設定' })).toHaveAttribute(
      'href',
      '/settings?section=ai_connection'
    );
  }
);

it('offers relogin for an expired account even when the helper is unavailable', async () => {
  const api = bridge();
  api.getState.mockResolvedValue({
    ...connection(),
    runtime: { ok: false, error: 'runtime_unavailable' },
  });
  render(subject('/history', 'expired'));
  await act(async () => {});
  expect(screen.getByRole('link', { name: '再ログイン' })).toHaveAttribute('href', '/login');
  expect(screen.queryByRole('link', { name: 'AI接続を設定' })).not.toBeInTheDocument();
});

it('leaves the detailed connection status to the AI settings page', async () => {
  bridge();
  render(subject('/settings?section=ai_connection'));
  await act(async () => {});
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
