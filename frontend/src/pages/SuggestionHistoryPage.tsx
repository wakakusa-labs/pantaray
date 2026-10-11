import { PanelLeft } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { ChatView, type ChatViewHandle } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import { HISTORY_SIDEBAR_ID, HistorySidebar } from '@/components/history/HistorySidebar';
import { NewTaskPane } from '@/components/task/NewTaskPane';
import { SuggestionTaskPane } from '@/components/task/SuggestionTaskPane';
import { TaskWorkspace } from '@/components/task/TaskWorkspace';
import { useI18n } from '@/context/useI18n';
import { useWorkspaceSettingsController } from '@/pages/settings/useWorkspaceSettingsController';
import {
  historyItemSelection,
  historySelectionSearch,
  parseHistorySelection,
  splitWorkKey,
  type HistorySelection,
} from '@/history/historySelection';
import { readShowChatState, showChatState } from '@/history/historyViewMode';
import { useSuggestionHistory } from '@/hooks/useSuggestionHistory';

import './suggestionHistoryPage.css';

// A task the loaded list does not hold yet (still loading, filtered out, or older than its pages).
const UNLISTED_TITLE = {
  en: { action: 'Task', suggestion: 'Suggestion' },
  ja: { action: '作業', suggestion: '提案' },
} as const;

/** Per viewer and only a convenience: without storage the sidebar starts shown. */
const SIDEBAR_HIDDEN_STORAGE_KEY = 'pantaray.history.sidebarHidden';

function readSidebarHidden(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_HIDDEN_STORAGE_KEY) === '1';
  } catch (error) {
    // Storage blocked or unavailable throws a DOMException.
    if (error instanceof DOMException) return false;
    throw error;
  }
}

function saveSidebarHidden(hidden: boolean) {
  try {
    localStorage.setItem(SIDEBAR_HIDDEN_STORAGE_KEY, hidden ? '1' : '0');
  } catch (error) {
    // Without storage the choice lasts until the page is left.
    if (!(error instanceof DOMException)) throw error;
  }
}

/** ⌘B on macOS, Ctrl+B elsewhere, with no other modifier. */
function isSidebarShortcut(event: KeyboardEvent, isMac: boolean): boolean {
  const primary = isMac ? event.metaKey : event.ctrlKey;
  const other = isMac ? event.ctrlKey : event.metaKey;
  return primary && !other && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'b';
}

/**
 * History: the sidebar of the chat and the user's tasks, and the detail pane of the one selected.
 * The selection is the URL's `?item=`, so it survives a reload and the back button returns to it.
 * A toggle in the title bar row (and ⌘B / Ctrl+B) hides the sidebar so the detail pane widens.
 */
const SuggestionHistoryPage = () => {
  const { chat, composer } = useChatSession();
  const history = useSuggestionHistory();
  const { language, t } = useI18n();
  const workspace = useWorkspaceSettingsController(t);
  const location = useLocation();
  const navigate = useNavigate();
  const selection = parseHistorySelection(location.search);
  // `Layout` and the task pane bring the page here with the Action to show in the chat.
  const reveal = readShowChatState(location.state, location.key);
  const chatView = useRef<ChatViewHandle>(null);
  const isMac = window.electron?.process.platform === 'darwin';

  const [sidebarHidden, setSidebarHidden] = useState(readSidebarHidden);
  const sidebarToggle = useRef<HTMLButtonElement>(null);
  const toggleSidebar = useCallback(() => {
    const hidden = !sidebarHidden;
    // Focus inside a hidden sidebar would fall back to the page's start.
    if (hidden && document.getElementById(HISTORY_SIDEBAR_ID)?.contains(document.activeElement))
      sidebarToggle.current?.focus();
    setSidebarHidden(hidden);
    saveSidebarHidden(hidden);
  }, [sidebarHidden]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || !isSidebarShortcut(event, isMac)) return;
      // A modal dialog (the sidebar's delete confirmation among them) leaves the page inert
      // behind it; hiding the sidebar would hide that dialog too.
      if (event.target instanceof Element && event.target.closest('dialog')) return;
      event.preventDefault();
      toggleSidebar();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isMac, toggleSidebar]);
  const sidebarToggleLabel = t(sidebarHidden ? 'history.sidebar.show' : 'history.sidebar.hide');

  const select = (next: HistorySelection, options?: { replace: true }) => {
    // The chat already shown goes back to its newest message instead.
    if (next === 'chat' && selection === 'chat') {
      chatView.current?.showNewest();
      return;
    }
    navigate({ search: historySelectionSearch(next) }, options);
  };
  const showInChat = (actionId: string) =>
    navigate({ search: historySelectionSearch('chat') }, { state: showChatState(actionId) });
  // The composers' Add project starts naming one in the sidebar, as its ＋ does.
  const [creatingProject, setCreatingProject] = useState(false);
  const addProject = () => setCreatingProject(true);
  // The Overlay's Add project brings the main window here with `?project=new`; the request is
  // taken while rendering, and the address then drops it.
  const projectRequested = new URLSearchParams(location.search).get('project') === 'new';
  if (projectRequested && !creatingProject) setCreatingProject(true);
  useEffect(() => {
    if (projectRequested) navigate({ search: '' }, { replace: true });
  }, [projectRequested, navigate]);

  const renderDetail = () => {
    if (selection === 'chat')
      return (
        <ChatView
          ref={chatView}
          notice={<AiConnectionNotice />}
          chat={chat}
          composer={composer}
          reveal={reveal}
          onOpenWork={select}
          onAddProject={addProject}
        />
      );
    if (selection === 'new')
      return (
        <NewTaskPane
          onStarted={(actionId) => select(`action:${actionId}`, { replace: true })}
          onAddProject={addProject}
        />
      );
    const { kind, id } = splitWorkKey(selection);
    const title =
      history.items.find((item) => historyItemSelection(item) === selection)?.title ??
      UNLISTED_TITLE[language][kind];
    if (kind === 'action')
      return (
        <TaskWorkspace
          key={id}
          actionId={id}
          title={title}
          onShowInChat={() => showInChat(id)}
          onAddProject={addProject}
        />
      );
    return (
      <SuggestionTaskPane
        key={id}
        suggestionId={id}
        title={title}
        onStarted={(actionId) => select(`action:${actionId}`, { replace: true })}
        onShowInChat={() => select('chat')}
        onAddProject={addProject}
      />
    );
  };

  return (
    <div className={sidebarHidden ? 'history-page history-page--sidebar-hidden' : 'history-page'}>
      <button
        ref={sidebarToggle}
        type="button"
        className="history-sidebar-toggle"
        aria-label={sidebarToggleLabel}
        data-tooltip={sidebarToggleLabel}
        aria-expanded={!sidebarHidden}
        aria-controls={HISTORY_SIDEBAR_ID}
        aria-keyshortcuts={isMac ? 'Meta+B' : 'Control+B'}
        onClick={toggleSidebar}
      >
        <PanelLeft size={17} strokeWidth={1.8} aria-hidden="true" />
      </button>
      <HistorySidebar
        history={history}
        projects={workspace}
        creatingProject={creatingProject}
        onCreatingProjectChange={setCreatingProject}
        selected={selection}
        onSelect={select}
        hidden={sidebarHidden}
      />
      <div className="history-detail">{renderDetail()}</div>
    </div>
  );
};

export default SuggestionHistoryPage;
