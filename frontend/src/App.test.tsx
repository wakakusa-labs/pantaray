import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import App from './App';
import { AuthContext, type AuthContextType } from './context/AuthContextDef';
import { UiLanguageProvider } from './context/UiLanguageContext';
import type { OnboardingState } from '../electron/src/ipc/schemas/onboarding';

vi.mock('../electron/src/auth/accountLoginFeature', () => ({
  PANTARAY_ACCOUNT_LOGIN_ENABLED: true,
}));

const pageMocks = vi.hoisted(() => ({
  organizations: [] as { organization_id: string; display_name: string }[],
  todoUx: true,
}));
vi.mock('./todos/todoUxFeature', () => ({
  get TODO_UX_ENABLED() {
    return pageMocks.todoUx;
  },
}));
// The home page hands the rail the organizations it read, as the real one does.
vi.mock('./pages/SuggestionHistoryPage', async () => {
  const { useEffect } = await import('react');
  const { useOutletContext } = await import('react-router-dom');
  return {
    default: function HomePage() {
      const { publishOrganizations } =
        useOutletContext<import('./components/Layout').LayoutOutletContext>();
      useEffect(() => publishOrganizations(pageMocks.organizations), [publishOrganizations]);
      return <h1>Local history</h1>;
    },
  };
});
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
const getState = vi.fn(async (): Promise<OnboardingState> => ({ status: 'completed' }));

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
    value: { ipcRenderer: { send }, auth: { startBrowserLogin }, onboarding: { getState } },
  });
  vi.stubEnv('VITE_WEB_APP_URL', 'https://app.example.com');
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  pageMocks.organizations = [];
  pageMocks.todoUx = true;
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
  // The rail holds the organizations to see TODOs for, then Settings; projects live in the
  // home page's sidebar.
  expect(
    within(screen.getByRole('navigation'))
      .getAllByRole('button', { name: /^(すべての組織|組織なし|設定|ホーム|ワークスペース)$/ })
      .map((button) => button.getAttribute('aria-label'))
  ).toEqual(['すべての組織', '組織なし', '設定']);
  expect(startBrowserLogin).not.toHaveBeenCalled();
});

it('opens first-run setup instead of the app, without the rail, until it is completed', async () => {
  getState.mockResolvedValueOnce({ status: 'in_progress', step: 'ai' });
  window.history.replaceState(null, '', '/#/history');
  render(subject());
  expect(await screen.findByRole('heading', { name: /AI をつなぐ/ })).toBeVisible();
  expect(screen.queryByRole('heading', { name: 'Local history' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'すべての組織' })).not.toBeInTheDocument();
});

it('opens a scope’s TODOs from the rail, and from another page returns to where home was left', async () => {
  const user = userEvent.setup();
  window.history.replaceState(null, '', '/#/history?item=action:task-1');
  render(subject());
  const rail = within(await screen.findByRole('navigation'));
  const all = rail.getByRole('button', { name: 'すべての組織' });
  expect(all).toHaveAttribute('aria-pressed', 'true');

  await user.click(rail.getByRole('button', { name: '設定' }));
  expect(await screen.findByRole('heading', { name: 'Local settings' })).toBeVisible();
  await user.click(all);
  expect(await screen.findByRole('heading', { name: 'Local history' })).toBeVisible();
  expect(window.location.hash).toBe('#/history?item=action:task-1');

  await user.click(rail.getByRole('button', { name: '組織なし' }));
  expect(window.location.hash).toBe('#/history?scope=none');
  expect(rail.getByRole('button', { name: '組織なし' })).toHaveAttribute('aria-pressed', 'true');
  await user.click(all);
  expect(window.location.hash).toBe('#/history');
});

it('without the TODO list, the rail holds Home and Settings, and Home reopens what History last showed', async () => {
  pageMocks.todoUx = false;
  const user = userEvent.setup();
  window.history.replaceState(null, '', '/#/history?item=action:task-1');
  render(subject());
  const rail = within(await screen.findByRole('navigation'));
  expect(
    rail
      .getAllByRole('button', { name: /^(すべての組織|組織なし|設定|ホーム|ワークスペース)$/ })
      .map((button) => button.getAttribute('aria-label'))
  ).toEqual(['ホーム', '設定']);
  const home = rail.getByRole('button', { name: 'ホーム' });
  expect(home).toHaveAttribute('aria-current', 'page');
  // On History, Home leaves the selection as it is.
  await user.click(home);
  expect(window.location.hash).toBe('#/history?item=action:task-1');

  await user.click(rail.getByRole('button', { name: '設定' }));
  expect(await screen.findByRole('heading', { name: 'Local settings' })).toBeVisible();
  await user.click(home);
  expect(await screen.findByRole('heading', { name: 'Local history' })).toBeVisible();
  expect(window.location.hash).toBe('#/history?item=action:task-1');
});

it('shows the organizations the home page read in the rail, each opening its TODOs', async () => {
  const user = userEvent.setup();
  pageMocks.organizations = [{ organization_id: 'O1', display_name: 'xer' }];
  window.history.replaceState(null, '', '/#/history');
  render(subject());
  const entry = await screen.findByRole('button', { name: 'xer' });
  expect(entry).toHaveTextContent('X');
  await user.click(entry);
  expect(window.location.hash).toBe('#/history?scope=organization:O1');
  expect(entry).toHaveAttribute('aria-pressed', 'true');
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
