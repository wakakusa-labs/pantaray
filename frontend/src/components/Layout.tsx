import React, { useState, useRef, useEffect, useMemo } from 'react';
import { Settings, UserRound } from 'lucide-react';
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
import { useShowHistoryItem } from '@/history/useShowHistoryItem';
import { OrganizationRail } from '@/components/todo/OrganizationRail';
import type { WorkspaceOrganization } from '@/pages/settings/components/workspaceSettingsModel';
import {
  DEFAULT_TODO_SELECTION,
  parseTodoSelection,
  todoSelectionSearch,
} from '@/todos/todoSelection';
import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import './Layout.css';

const HOME_PATH = '/history';

/** What the home page hands the shell: the organizations its settings read, for the rail. */
export type LayoutOutletContext = {
  publishOrganizations: (organizations: readonly WorkspaceOrganization[]) => void;
};

const sameScope = (a: TodoScopeQuery, b: TodoScopeQuery) =>
  a.scope === b.scope && a.organization_id === b.organization_id;

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
  // The home page reads the workspace on every visit and hands its organizations up; until then
  // the rail holds the entries every workspace has.
  const [organizations, setOrganizations] = useState<readonly WorkspaceOrganization[]>([]);
  const outletContext = useMemo<LayoutOutletContext>(
    () => ({ publishOrganizations: setOrganizations }),
    []
  );
  // The rail's scope reopens what the home page last showed, for as long as the window is open.
  const onHome = location.pathname === HOME_PATH;
  const [lastHomeSearch, setLastHomeSearch] = useState('');
  if (onHome && lastHomeSearch !== location.search) setLastHomeSearch(location.search);
  const scope = parseTodoSelection(onHome ? location.search : lastHomeSearch).scope;

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
  const isSplitPage = onHome || location.pathname === '/settings';

  // From another page, the scope in view returns to where the home page was left; any other
  // choice opens that scope's TODOs.
  const openScope = (next: TodoScopeQuery) => {
    if (!onHome && sameScope(next, scope))
      navigate({ pathname: HOME_PATH, search: lastHomeSearch });
    else
      navigate({
        pathname: HOME_PATH,
        search: todoSelectionSearch({ ...DEFAULT_TODO_SELECTION, scope: next }),
      });
  };
  const settingsActive = location.pathname === '/settings';

  return (
    <ChatUnreadContext.Provider value={chatUnread}>
      <div className="app-shell">
        {/* The window has no title bar; this strip above the content is what drags it. */}
        <div className="app-titlebar" aria-hidden="true" />
        <nav className="app-rail">
          {/* The home: the TODOs of all organizations, of one, or of none. */}
          <div className="app-rail-scopes">
            <OrganizationRail organizations={organizations} selected={scope} onSelect={openScope} />
            {/* The chat lives on the home page; its unread replies mark the home from any page. */}
            {chatUnread > 0 ? (
              <span
                className="app-rail-unread-dot"
                role="img"
                aria-label={t('history.chat.unreadInRail', { count: chatUnread })}
              />
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => navigate('/settings')}
            className={['app-rail-item', settingsActive ? 'app-rail-item--active' : null]
              .filter(Boolean)
              .join(' ')}
            aria-label={t('nav.settings')}
            data-tooltip={t('nav.settings')}
            aria-current={settingsActive ? 'page' : undefined}
          >
            <Settings size={19} strokeWidth={1.8} aria-hidden="true" />
          </button>
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
            <Outlet context={outletContext} />
          </div>
          <UpdateReadyNotice />
        </main>
      </div>
    </ChatUnreadContext.Provider>
  );
};

export default Layout;
