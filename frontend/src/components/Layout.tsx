import React, { useState, useRef, useEffect } from 'react';
import { Clock, Folder, Settings, UserRound } from 'lucide-react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { useI18n } from '@/context/useI18n';
import { RecordingIntroDialog } from './RecordingIntroDialog';
import { RecordingRailControl } from './RecordingRailControl';
import { AiConnectionNotice } from './AiConnectionNotice';
import { LocalOwnerBoundary } from './LocalOwnerBoundary';
import { UpdateReadyNotice } from './UpdateReadyNotice';
import { PANTARAY_ACCOUNT_LOGIN_ENABLED } from '../../electron/src/auth/accountLoginFeature';
import './Layout.css';

/**
 * The main window's shell: an icon rail on the left on every page, and the page beside it.
 */
const Layout: React.FC = () => {
  const { user, signOut, authStatus, runtimeState } = useAuth();
  const needsLogin = authStatus === 'expired';
  const hasAccount = authStatus === 'authenticated' || needsLogin;
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsDropdownOpen(false);
      }
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsDropdownOpen(false);
        menuButtonRef.current?.focus();
      }
    };

    if (isDropdownOpen) {
      document.addEventListener('keydown', handleKeyDown);
      document.addEventListener('mousedown', handleClickOutside);
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isDropdownOpen]);

  const handleLogout = async () => {
    setIsDropdownOpen(false);
    const { error } = await signOut();
    if (error) {
      console.error('Error during logout:', error);
    }
  };

  const getUserInitials = (email: string | undefined): string => {
    if (!email) return 'U';
    const parts = email.split('@')[0].split('.');
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return email[0].toUpperCase();
  };

  // A page with its own columns shows the AI-connection notice in its right column. Workspace
  // has those columns only once the local owner it belongs to is published.
  const isSplitPage =
    location.pathname === '/settings' ||
    (location.pathname === '/workspace' &&
      runtimeState.status === 'ready' &&
      runtimeState.owner !== null);

  const navItems = [
    { path: '/history', label: t('nav.history'), Icon: Clock },
    { path: '/workspace', label: t('settings.workspace.title'), Icon: Folder },
    { path: '/settings', label: t('nav.settings'), Icon: Settings },
  ];

  return (
    <div className="app-shell">
      {/* The window has no title bar; this strip above the content is what drags it. */}
      <div className="app-titlebar" aria-hidden="true" />
      <nav className="app-rail">
        {navItems.map(({ path, label, Icon }) => {
          const isActive = location.pathname === path;
          return (
            <button
              key={path}
              type="button"
              onClick={() => navigate(path)}
              className={['app-rail-item', isActive ? 'app-rail-item--active' : null]
                .filter(Boolean)
                .join(' ')}
              aria-label={label}
              title={label}
              aria-current={isActive ? 'page' : undefined}
            >
              <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
            </button>
          );
        })}
        <div className="app-rail-bottom">
          {/* Recording belongs to the local owner; without one there is nothing to show. */}
          <LocalOwnerBoundary fallback={null}>
            <RecordingRailControl />
          </LocalOwnerBoundary>
          {PANTARAY_ACCOUNT_LOGIN_ENABLED && (
            <div className="app-rail-user" ref={dropdownRef}>
              <button
                type="button"
                ref={menuButtonRef}
                aria-expanded={isDropdownOpen}
                aria-controls="app-user-dropdown"
                className="app-user-button"
                onClick={() => setIsDropdownOpen(!isDropdownOpen)}
                aria-label={t('layout.userMenuAriaLabel')}
              >
                {user ? getUserInitials(user.email) : <UserRound size={18} aria-hidden="true" />}
              </button>
              {isDropdownOpen && (
                <div className="app-user-dropdown" id="app-user-dropdown">
                  {user?.email && <div className="app-user-dropdown-email">{user.email}</div>}
                  {needsLogin && (
                    <p className="app-user-dropdown-email" role="status">
                      {t('layout.sessionExpired')}
                    </p>
                  )}
                  {!user && (
                    <Link
                      className="app-user-dropdown-item"
                      to="/login"
                      onClick={() => setIsDropdownOpen(false)}
                    >
                      {t(needsLogin ? 'menu.reauthenticate' : 'menu.login')}
                    </Link>
                  )}
                  {hasAccount && (
                    <button className="app-user-dropdown-item" onClick={handleLogout}>
                      {t('menu.logout')}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </nav>

      {/* Recording belongs to the local owner; without one there is nothing to ask for. */}
      <LocalOwnerBoundary fallback={null}>
        <RecordingIntroDialog />
      </LocalOwnerBoundary>

      <main className="app-main">
        <div className={isSplitPage ? 'app-surface app-surface--split' : 'app-surface'}>
          {isSplitPage ? null : <AiConnectionNotice />}
          <Outlet />
        </div>
        <UpdateReadyNotice />
      </main>
    </div>
  );
};

export default Layout;
