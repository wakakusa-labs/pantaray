import React, { useState, useRef, useEffect } from 'react';
import { Clock, Settings, UserRound } from 'lucide-react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { useI18n } from '@/context/useI18n';
import { RecordingIntroDialog } from './RecordingIntroDialog';
import { RecordingRailControl } from './RecordingRailControl';
import { AiConnectionNotice } from './AiConnectionNotice';
import { LocalOwnerBoundary } from './LocalOwnerBoundary';
import { ChatUnreadTracker } from './chat/ChatUnreadTracker';
import { ChatUnreadContext } from './chat/chatUnread';
import { UpdateReadyNotice } from './UpdateReadyNotice';
import { PANTARAY_ACCOUNT_LOGIN_ENABLED } from '../../electron/src/auth/accountLoginFeature';
import { showChatState } from '@/history/historyViewMode';
import { useShowHistoryItem } from '@/history/useShowHistoryItem';
import './Layout.css';

/**
 * The main window's shell: an icon rail on the left on every page, and the page beside it.
 */
const Layout: React.FC = () => {
  const { user, signOut, authStatus } = useAuth();
  const needsLogin = authStatus === 'expired';
  const hasAccount = authStatus === 'authenticated' || needsLogin;
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [chatUnread, setChatUnread] = useState(0);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();

  // The Overlay's chat button: from any page, open History's chat on that Action's latest card.
  useEffect(() => {
    const onShowChat = window.electron?.history?.onShowChat;
    if (!onShowChat) return;
    return onShowChat(({ actionId }) => {
      navigate('/history', { state: showChatState(actionId) });
    });
  }, [navigate]);
  useShowHistoryItem();

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

  // A page with its own columns shows the AI-connection notice in its right column.
  const isSplitPage = location.pathname === '/history' || location.pathname === '/settings';

  const navItems = [
    { path: '/history', label: t('nav.history'), Icon: Clock },
    { path: '/settings', label: t('nav.settings'), Icon: Settings },
  ];

  return (
    <ChatUnreadContext.Provider value={chatUnread}>
      <div className="app-shell">
        {/* The window has no title bar; this strip above the content is what drags it. */}
        <div className="app-titlebar" aria-hidden="true" />
        <nav className="app-rail">
          {navItems.map(({ path, label, Icon }) => {
            const isActive = location.pathname === path;
            const unread = path === '/history' && chatUnread > 0;
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
                aria-describedby={unread ? 'app-rail-chat-unread' : undefined}
              >
                <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
                {unread ? <span className="app-rail-unread-dot" aria-hidden="true" /> : null}
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
        {chatUnread > 0 ? (
          <span id="app-rail-chat-unread" hidden>
            {t('history.chat.unread', { count: chatUnread })}
          </span>
        ) : null}

        {/* The chat's unread count, for the rail and History's chat row on every page. */}
        <LocalOwnerBoundary fallback={null}>
          <ChatUnreadTracker onCount={setChatUnread} />
        </LocalOwnerBoundary>

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
    </ChatUnreadContext.Provider>
  );
};

export default Layout;
