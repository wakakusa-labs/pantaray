import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import App from './App';
import { AuthContext, type AuthContextType } from './context/AuthContextDef';
import { UiLanguageProvider } from './context/UiLanguageContext';

vi.mock('../electron/src/auth/accountLoginFeature', () => ({
  PANTARAY_ACCOUNT_LOGIN_ENABLED: true,
}));

vi.mock('./pages/SuggestionHistoryPage', () => ({ default: () => <h1>Local history</h1> }));
vi.mock('./pages/SettingsPage', () => ({ default: () => <h1>Local settings</h1> }));
vi.mock('./components/RecordingIntroDialog', () => ({ RecordingIntroDialog: () => null }));
vi.mock('./lib/supabase', () => ({
  getSupabase: () => ({
    auth: {
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => undefined } } }),
      getSession: async () => ({ data: { session: null }, error: null }),
    },
  }),
}));

let auth: AuthContextType;
const startBrowserLogin = vi.fn(async () => ({
  ok: true,
  attempt_id: 'test-attempt',
  code_challenge: 'test-challenge',
}));
const send = vi.fn();

function subject() {
  return (
    <AuthContext.Provider value={auth}>
      <UiLanguageProvider initialLanguage="ja">
        <App />
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}

beforeEach(() => {
  auth = {
    authStatus: 'unauthenticated',
    user: null,
    session: null,
    loading: false,
    runtimeState: { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
    signIn: vi.fn(async () => ({ error: null })),
    signUp: vi.fn(async () => ({ status: 'created' as const, error: null })),
    signOut: vi.fn(async () => ({ error: null })),
    resetPassword: vi.fn(async () => ({ error: null })),
  };
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: { ipcRenderer: { send }, auth: { startBrowserLogin } },
  });
  vi.stubEnv('VITE_WEB_APP_URL', 'https://app.example.com');
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  delete window.electron;
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

it.each([
  ['/', 'Local history'],
  ['/history', 'Local history'],
  ['/settings', 'Local settings'],
])('opens desktop %s without a Pantaray account', async (path, heading) => {
  window.history.replaceState(null, '', `/#${path}`);
  render(subject());
  expect(await screen.findByRole('heading', { name: heading })).toBeVisible();
  // Projects live in History's sidebar and the workspace in Settings; the rail has no page for them.
  expect(
    within(screen.getByRole('navigation')).getAllByRole('button', {
      name: /^(履歴|設定|ワークスペース)$/,
    })
  ).toHaveLength(2);
  expect(startBrowserLogin).not.toHaveBeenCalled();
});

it('offers optional Pantaray login and lets a guest return without starting authentication', async () => {
  const user = userEvent.setup();
  render(subject());
  await user.click(await screen.findByRole('button', { name: 'ユーザーメニュー' }));
  expect(screen.queryByRole('button', { name: 'ログアウト' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('link', { name: 'Pantarayにログイン' }));
  expect(await screen.findByRole('heading', { name: 'Pantarayログイン' })).toBeVisible();
  await user.click(screen.getByRole('link', { name: 'アプリに戻る' }));
  expect(await screen.findByRole('heading', { name: 'Local history' })).toBeVisible();
  expect(startBrowserLogin).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});

it('starts browser authentication only after the login action and returns on completion', async () => {
  const user = userEvent.setup();
  window.history.replaceState(null, '', '/#/login');
  const view = render(subject());
  await user.click(screen.getByRole('button', { name: 'ブラウザでログイン' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(startBrowserLogin).toHaveBeenCalledWith('login');
  const [channel, url] = send.mock.calls[0];
  expect(channel).toBe('open-external-url');
  expect(new URL(url).searchParams.get('attempt_id')).toBe('test-attempt');
  auth = {
    ...auth,
    authStatus: 'authenticated',
    user: {
      id: 'alice',
      email: 'alice@example.com',
      aud: 'authenticated',
      app_metadata: {},
      user_metadata: {},
      created_at: '2026-09-17',
    },
    runtimeState: { status: 'ready', message: null, owner: { kind: 'account', id: 'alice' } },
  };
  view.rerender(subject());
  expect(await screen.findByRole('heading', { name: 'Local history' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'ユーザーメニュー' }));
  expect(screen.getByRole('button', { name: 'ログアウト' })).toBeVisible();
  expect(screen.queryByRole('link', { name: 'Pantarayにログイン' })).not.toBeInTheDocument();
});

it.each([
  { status: 'ready', message: null, owner: { kind: 'account', id: 'alice' } },
  { status: 'degraded', message: 'helper unavailable', owner: null },
] satisfies AuthContextType['runtimeState'][])(
  'keeps expired account relogin and logout available with a $status runtime',
  async (runtimeState) => {
    const user = userEvent.setup();
    auth.authStatus = 'expired';
    auth.runtimeState = runtimeState;
    window.history.replaceState(null, '', '/#/settings');
    render(subject());
    expect(await screen.findByRole('heading', { name: 'Local settings' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'ユーザーメニュー' }));
    expect(screen.getByText('Pantarayへの再ログインが必要です。')).toBeVisible();
    expect(screen.getByRole('link', { name: 'Pantarayに再ログイン' })).toHaveAttribute(
      'href',
      '#/login'
    );
    await user.click(screen.getByRole('button', { name: 'ログアウト' }));
    expect(auth.signOut).toHaveBeenCalledTimes(1);
  }
);

it('closes the account disclosure with Escape and restores its trigger focus', async () => {
  const user = userEvent.setup();
  render(subject());
  const trigger = await screen.findByRole('button', { name: 'ユーザーメニュー' });
  await user.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await user.tab();
  await user.keyboard('{Escape}');
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(trigger).toHaveFocus();
  expect(screen.queryByRole('link', { name: 'Pantarayにログイン' })).not.toBeInTheDocument();
});

it.each(['/history', '/settings'])('keeps browser %s behind the desktop boundary', async (path) => {
  delete window.electron;
  window.history.replaceState(null, '', path);
  render(subject());
  expect(
    await screen.findByRole('heading', { name: 'デスクトップアプリが必要です' })
  ).toBeVisible();
  expect(screen.queryByRole('heading', { name: /^Local / })).not.toBeInTheDocument();
});

it('does not expose the desktop app to an authenticated browser user', async () => {
  delete window.electron;
  auth.authStatus = 'authenticated';
  auth.user = {
    id: 'alice',
    aud: 'authenticated',
    app_metadata: {},
    user_metadata: {},
    created_at: '2026-09-17',
  };
  window.history.replaceState(null, '', '/settings');
  render(subject());
  expect(
    await screen.findByRole('heading', { name: 'デスクトップアプリが必要です' })
  ).toBeVisible();
  expect(screen.queryByRole('heading', { name: 'Local settings' })).not.toBeInTheDocument();
});
