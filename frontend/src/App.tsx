import React, { useEffect } from 'react';
import {
  HashRouter,
  BrowserRouter,
  Outlet,
  Routes,
  Route,
  Navigate,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import { useAuth } from './hooks/useAuth';
import LoginPage from './pages/LoginPage';
import SignUpPage from './pages/SignUpPage';
import ForgotPasswordPage from './pages/ForgotPasswordPage';
import ResetPasswordPage from './pages/ResetPasswordPage';
import SettingsPage from './pages/SettingsPage';
import ConfirmationSuccess from './pages/ConfirmationSuccess';
import PasswordResetSuccess from './pages/PasswordResetSuccess';
import EmailVerificationPage from './pages/EmailVerificationPage';
import { getSupabase } from './lib/supabase';
import Layout from './components/Layout';
import { LocalOwnerBoundary } from './components/LocalOwnerBoundary';
import SuggestionHistoryPage from './pages/SuggestionHistoryPage';
import { ChatSessionProvider } from './components/chat/ChatSessionProvider';
import WorkspacePage from './pages/WorkspacePage';
import DesktopAppOnlyPage from './pages/DesktopAppOnlyPage';
import { useI18n } from '@/context/useI18n';
import { BrandWordmark } from '@/components/BrandWordmark';
import { PANTARAY_ACCOUNT_LOGIN_ENABLED } from '../electron/src/auth/accountLoginFeature';

const AccountLoginDisabledPage: React.FC = () => {
  const { t } = useI18n();
  return (
    <div className="auth-container">
      <div className="auth-form">
        <BrandWordmark className="brand-wordmark--auth" />
        <h2>{t('auth.login.disabledTitle')}</h2>
        <p>{t('auth.login.disabledBody')}</p>
      </div>
    </div>
  );
};

// ページに応じてbodyのクラスとウィンドウサイズを設定するコンポーネント
const PageClassManager: React.FC = () => {
  const location = useLocation();

  useEffect(() => {
    // 特定のページごとの処理が必要な場合はここに追加

    // 常に透明化クラスを削除する
    document.body.classList.remove('transparent-bg');

    // ブラウザログイン→Desktop復帰のフラグを保持する（URLのsearchを利用）
    // - Electron では不要
    try {
      const isElectron = typeof window !== 'undefined' && !!window.electron;
      if (!isElectron && PANTARAY_ACCOUNT_LOGIN_ENABLED) {
        const params = new URLSearchParams(location.search || '');
        const desktop = params.get('desktop');
        if (desktop === '1' || desktop === 'true') {
          localStorage.setItem('pantaray_desktop_return', '1');
        }
      }
    } catch {
      // no-op
    }
  }, [location]);

  return null;
};

// 認証状態の変更を監視するコンポーネント
const AuthStateManager: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    // Electron は main が認証状態を管理するため、renderer で Supabase の auth を購読しない
    try {
      const isElectron = typeof window !== 'undefined' && !!window.electron;
      if (isElectron) {
        return;
      }
    } catch {
      // no-op
    }

    // 認証状態の変更を監視
    const {
      data: { subscription },
    } = getSupabase().auth.onAuthStateChange((_event, session) => {
      console.log('Auth state changed in AuthStateManager:', {
        event: _event,
        hasSession: !!session,
        path: location.pathname,
      });

      // リダイレクトをスキップするフラグをチェック
      const skipRedirect = localStorage.getItem('skipAuthRedirect') === 'true';

      // パスワードリセット関連のパスであればリダイレクトをスキップ
      const isPasswordResetPath =
        location.pathname === '/reset-password' || location.pathname === '/password-reset-success';

      // desktop=1（Desktop復帰フロー）の場合は、ログイン直後に/historyへ飛ばさない。
      // ここで遷移してしまうと、ログイン画面側の「Open Desktop App」導線が消えてしまい
      // deep link がブラウザにブロックされた時に復帰できなくなる。
      let isDesktopReturn = false;
      try {
        const params = new URLSearchParams(location.search || '');
        const desktop = params.get('desktop');
        isDesktopReturn = desktop === '1' || desktop === 'true';
      } catch {
        // no-op
      }

      // スキップすべき条件：
      // 1. skipAuthRedirectフラグが設定されている、または
      // 2. パスワードリセット関連のパスにいる
      const shouldSkipRedirect = skipRedirect || isPasswordResetPath || isDesktopReturn;

      // Electron で「勝手に /history に戻る」事故を防ぐため、
      // 自動遷移は「認証画面からログイン完了した場合」に限定する。
      const isAuthEntryPath =
        location.pathname === '/' ||
        location.pathname === '/login' ||
        location.pathname === '/signup' ||
        location.pathname === '/forgot-password';

      // 認証状態が変更された場合の処理
      if (_event === 'SIGNED_IN') {
        // ブラウザではアプリ本体（/history 等）へ遷移させない（認証ポータル用途に限定する）
        try {
          const isElectron = typeof window !== 'undefined' && !!window.electron;
          if (!isElectron) {
            return;
          }
        } catch {
          // no-op
        }
        // リダイレクトをスキップすべきでなく、かつ認証画面からの遷移であれば履歴へ
        if (!shouldSkipRedirect && isAuthEntryPath) {
          navigate('/history');
        }
      } else if (_event === 'SIGNED_OUT') {
        // パスワードリセット関連のパスであればリダイレクトしない
        if (!isPasswordResetPath) {
          navigate('/login');
        }
      } else if (_event === 'USER_UPDATED') {
        // ユーザー情報が更新された場合（メール確認など）
        if (session?.user.email_confirmed_at) {
          // 確認メールからのリダイレクトの場合は確認完了ページに遷移
          // URLにハッシュフラグメントが含まれているかチェック
          const hasAuthParams =
            window.location.hash.includes('access_token') || window.location.hash.includes('error');

          const isPasswordRecovery = window.location.hash.includes('type=recovery');

          if (isPasswordRecovery) {
            // パスワードリセットリンクの場合はパスワード再設定ページに遷移
            navigate('/reset-password');
          } else if (hasAuthParams && location.pathname !== '/confirmation-success') {
            navigate('/confirmation-success');
          } else if (!hasAuthParams && !shouldSkipRedirect && isAuthEntryPath) {
            // リダイレクトをスキップすべきでなければ履歴ページへ
            navigate('/history');
          }
        }
      }
    });

    return () => subscription.unsubscribe();
  }, [navigate, location]);

  return null;
};

// Desktop はローカル所有者で動作し、Browser は認証ポータルだけを公開する。
const MainAppRoutes: React.FC = () => {
  const { user, loading } = useAuth();
  const isElectron = typeof window !== 'undefined' && !!window.electron;
  const accountPageDisabled = !PANTARAY_ACCOUNT_LOGIN_ENABLED;
  const { t } = useI18n();

  // ローディング中の表示
  if (loading) {
    return <div className="app-boot-loading">{t('common.loading')}</div>;
  }

  return (
    <>
      {/* Browser のみ: 認証ポータル用途の補助（Desktop=1 フローや password reset を扱う） */}
      {!isElectron && PANTARAY_ACCOUNT_LOGIN_ENABLED && <AuthStateManager />}
      <Routes>
        <Route
          element={
            accountPageDisabled ? (
              isElectron ? (
                <Navigate to="/history" replace />
              ) : (
                <AccountLoginDisabledPage />
              )
            ) : (
              <Outlet />
            )
          }
        >
          <Route
            path="/login"
            element={isElectron && user ? <Navigate to="/history" replace /> : <LoginPage />}
          />
          <Route
            path="/signup"
            element={isElectron && user ? <Navigate to="/history" replace /> : <SignUpPage />}
          />
          <Route
            path="/forgot-password"
            element={
              isElectron && user ? <Navigate to="/history" replace /> : <ForgotPasswordPage />
            }
          />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/confirmation-success" element={<ConfirmationSuccess />} />
          <Route path="/password-reset-success" element={<PasswordResetSuccess />} />
          <Route path="/email-verification" element={<EmailVerificationPage />} />
        </Route>

        {/* Layoutを適用したルート */}
        {isElectron ? (
          <Route element={<Layout />}>
            {/* Settings also holds installation-wide sections, and scopes its own. */}
            <Route path="/settings" element={<SettingsPage />} />
            <Route
              element={
                <LocalOwnerBoundary>
                  <ChatSessionProvider>
                    <Outlet />
                  </ChatSessionProvider>
                </LocalOwnerBoundary>
              }
            >
              <Route path="/history" element={<SuggestionHistoryPage />} />
              <Route path="/workspace" element={<WorkspacePage />} />
            </Route>
          </Route>
        ) : (
          <>
            {/* Browser からはアプリ本体ページを見せない */}
            <Route path="/settings" element={<DesktopAppOnlyPage />} />
            <Route path="/history" element={<DesktopAppOnlyPage />} />
            <Route path="/workspace" element={<DesktopAppOnlyPage />} />
          </>
        )}

        <Route path="/" element={<Navigate to={isElectron ? '/history' : '/login'} replace />} />
        <Route
          path="*"
          element={isElectron ? <Navigate to="/history" replace /> : <DesktopAppOnlyPage />}
        />
      </Routes>
    </>
  );
};

// Appコンポーネント (useAuth は呼び出さない)
const App: React.FC = () => {
  const isElectron = typeof window !== 'undefined' && !!window.electron;
  // Web: クリーンURL（BrowserRouter） / Electron: URLを露出させない（HashRouter）
  const RouterImpl = isElectron ? HashRouter : BrowserRouter;
  return (
    <RouterImpl>
      <PageClassManager /> {/* PageClassManager は常に表示 */}
      <MainAppRoutes />
    </RouterImpl>
  );
};

// デフォルトエクスポートは App のまま（AppWrapper は不要になった）
export default App;
