import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import App from './App';
import { AuthContext, type AuthContextType } from './context/AuthContextDef';
import { UiLanguageProvider } from './context/UiLanguageContext';

vi.mock('./pages/SuggestionHistoryPage', () => ({ default: () => <h1>Local history</h1> }));
vi.mock('./pages/SettingsPage', () => ({ default: () => <h1>Local settings</h1> }));
vi.mock('./components/RecordingIntroDialog', () => ({ RecordingIntroDialog: () => null }));
// The real Supabase module loads here: with account login off it needs no Supabase setting.

const auth: AuthContextType = {
  authStatus: 'unauthenticated',
  user: null,
  session: null,
  loading: false,
  runtimeState: { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
  signIn: vi.fn(),
  signUp: vi.fn(),
  signOut: vi.fn(),
  resetPassword: vi.fn(),
};

afterEach(() => {
  cleanup();
  delete window.electron;
});

it.each(['/login', '/signup', '/forgot-password', '/reset-password'])(
  'keeps disabled desktop account page %s inaccessible',
  async (path) => {
    Object.defineProperty(window, 'electron', { configurable: true, value: { auth: {} } });
    window.history.replaceState(null, '', `/#${path}`);
    render(
      <AuthContext.Provider value={auth}>
        <UiLanguageProvider initialLanguage="ja">
          <App />
        </UiLanguageProvider>
      </AuthContext.Provider>
    );

    expect(await screen.findByRole('heading', { name: 'Local history' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'ユーザーメニュー' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Pantarayログイン' })).not.toBeInTheDocument();
  }
);

it.each(['/login', '/signup', '/forgot-password', '/reset-password', '/history'])(
  'prevents browser account access through %s',
  async (path) => {
    window.history.replaceState(null, '', path);
    render(
      <AuthContext.Provider value={auth}>
        <UiLanguageProvider initialLanguage="ja">
          <App />
        </UiLanguageProvider>
      </AuthContext.Provider>
    );

    expect(
      await screen.findByRole('heading', { name: 'Pantarayアカウントのログインは停止中です' })
    ).toBeVisible();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ログイン' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'ログインへ移動' })).not.toBeInTheDocument();
  }
);
