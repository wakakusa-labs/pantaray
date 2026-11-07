import { PanelLeft } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useOutletContext } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import { ChatView, type ChatViewHandle } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import {
  HISTORY_SIDEBAR_ID,
  HistorySidebar,
  type TodoListPlace,
  type TodoSidebarNav,
} from '@/components/history/HistorySidebar';
import type { LayoutOutletContext } from '@/components/Layout';
import { NewTaskPane } from '@/components/task/NewTaskPane';
import { SuggestionTaskPane } from '@/components/task/SuggestionTaskPane';
import { TaskWorkspace } from '@/components/task/TaskWorkspace';
import type { TaskPreview } from '@/components/task/taskFiles';
import { useI18n } from '@/context/useI18n';
import { useWorkspaceSettingsController } from '@/pages/settings/useWorkspaceSettingsController';
import {
  historyItemSelection,
  historySelectionSearch,
  parseHistorySelection,
  parseRequestedFile,
  splitWorkKey,
  type HistorySelection,
} from '@/history/historySelection';
import { useSuggestionHistory } from '@/hooks/useSuggestionHistory';
import type { WorkspaceProject } from '@/pages/settings/components/workspaceSettingsModel';
import {
  DEFAULT_TODO_SELECTION,
  parseTodoSelection,
  todoSelectionSearch,
  type TodoProjectFilter,
  type TodoSelection,
} from '@/todos/todoSelection';
import { useTodoWeek } from '@/todos/useTodoWeek';

import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import { TodoScreen } from './TodoScreen';
import './suggestionHistoryPage.css';

// A task the loaded list does not hold yet (still loading, filtered out, or older than its pages).
const UNLISTED_TITLE = {
  en: { action: 'Task', suggestion: 'Suggestion' },
  ja: { action: '作業', suggestion: '提案' },
} as const;

/** The query parameter `historySelectionSearch` writes; without it the page shows the TODOs. */
const ITEM_PARAM = 'item';
/** The week is read whole: the sidebar counts every project's open TODOs. */
const ANY_PROJECT: TodoProjectFilter = { project: 'any' };

/**
 * The query for a conversation in the main area. The scope in view stays, so the rail keeps it
 * and the TODOs return to it.
 */
function itemSearch(item: HistorySelection, scope: TodoScopeQuery) {
  const scopeQuery = todoSelectionSearch({ ...DEFAULT_TODO_SELECTION, scope }).slice(1);
  const query = historySelectionSearch(item);
  return scopeQuery ? `${query}&${scopeQuery}` : query;
}

/** Whether a project's TODOs are among the scope's. */
function inScope(project: WorkspaceProject, scope: TodoScopeQuery): boolean {
  if (scope.scope === 'all') return true;
  if (scope.scope === 'no_organization') return project.organization_ids.length === 0;
  return project.organization_ids.some((id) => id === scope.organization_id);
}

/** Which TODO list the sidebar marks current. */
function listPlace(selection: TodoSelection): TodoListPlace | null {
  if (selection.view === 'deleted') return { kind: 'deleted' };
  const { project } = selection;
  if (project.project === 'project' && project.project_id !== undefined)
    return { kind: 'project', projectId: project.project_id };
  return project.project === 'any' ? { kind: 'all' } : null;
}

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
 * The home page: the sidebar, and beside it the TODO list with the selected TODO's conversation,
 * or, once the sidebar's chat or another conversation is chosen (`?item=`), that one alone, as
 * History always showed it. What is shown is the URL's query, so it survives a reload and the
 * back button returns to it. A toggle in the title bar row (and ⌘B / Ctrl+B) hides the sidebar
 * so the main area widens.
 */
const SuggestionHistoryPage = () => {
  const { chat, composer } = useChatSession();
  const history = useSuggestionHistory();
  const { language, t } = useI18n();
  const workspace = useWorkspaceSettingsController(t);
  const location = useLocation();
  const navigate = useNavigate();
  const { publishOrganizations } = useOutletContext<LayoutOutletContext>();
  const selection = new URLSearchParams(location.search).has(ITEM_PARAM)
    ? parseHistorySelection(location.search)
    : null;
  const todoSelection = parseTodoSelection(location.search);
  const { scope } = todoSelection;
  const week = useTodoWeek(scope, ANY_PROJECT, todoSelection.weekStart);
  const { settings } = workspace;

  // The rail's organizations are the ones this visit read.
  const organizations = settings?.organizations;
  useEffect(() => {
    if (organizations) publishOrganizations(organizations);
  }, [organizations, publishOrganizations]);
  // An organization deleted elsewhere leaves nothing to show in its scope: all of them show.
  const scopeGone =
    scope.scope === 'organization' &&
    organizations !== undefined &&
    !organizations.some((organization) => organization.organization_id === scope.organization_id);
  useEffect(() => {
    if (scopeGone)
      navigate({ search: todoSelectionSearch(DEFAULT_TODO_SELECTION) }, { replace: true });
  }, [scopeGone, navigate]);
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

  // The file open beside a suggestion stays open in the Action that takes the pane over from it.
  // It is handed over once: the suggestion it came from is spared only until the Action is shown,
  // and showing anything else drops it, however the address changed, so the Action shown again
  // later opens as the user left it.
  const [handedPreview, setHandedPreview] = useState<{
    from: HistorySelection | null;
    actionId: string;
    preview: TaskPreview;
  } | null>(null);
  if (handedPreview) {
    const handedTo = `action:${handedPreview.actionId}`;
    if (selection === handedTo && handedPreview.from !== null)
      setHandedPreview({ ...handedPreview, from: null });
    // The TODO list is never where the file came from.
    else if (selection !== handedTo && (selection === null || selection !== handedPreview.from))
      setHandedPreview(null);
  }
  const select = (next: HistorySelection, options?: { replace: true }) => {
    // The chat already shown goes back to its newest message instead.
    if (next === 'chat' && selection === 'chat') {
      chatView.current?.showNewest();
      return;
    }
    navigate({ search: itemSearch(next, scope) }, options);
  };
  const selectTodos = (next: TodoSelection) => navigate({ search: todoSelectionSearch(next) });
  const openCounts = new Map<string, number>();
  for (const todo of week.page?.items ?? []) {
    if (todo.project_id !== null && todo.state.kind !== 'done')
      openCounts.set(todo.project_id, (openCounts.get(todo.project_id) ?? 0) + 1);
  }
  const todoNav: TodoSidebarNav = {
    current: selection === null ? listPlace(todoSelection) : null,
    openCounts,
    onShowAll: () =>
      selectTodos({ ...DEFAULT_TODO_SELECTION, scope, weekStart: todoSelection.weekStart }),
    onShowDeleted: () => selectTodos({ ...DEFAULT_TODO_SELECTION, scope, view: 'deleted' }),
    onSelectProject: (projectId) => {
      const project = settings?.projects.find((candidate) => candidate.project_id === projectId);
      selectTodos({
        ...DEFAULT_TODO_SELECTION,
        // A project outside the scope in view is shown among all organizations'.
        scope: project && inScope(project, scope) ? scope : DEFAULT_TODO_SELECTION.scope,
        project: { project: 'project', project_id: projectId },
        weekStart: todoSelection.weekStart,
      });
    },
  };
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
  // A file an Overlay chip asked for is handed to the pane, then dropped from the address, so the
  // same chip pressed again asks once more.
  const requestedFile = parseRequestedFile(location.search);
  useEffect(() => {
    if (requestedFile !== null && selection !== null)
      navigate({ search: itemSearch(selection, scope) }, { replace: true });
  }, [requestedFile, selection, scope, navigate]);

  const renderDetail = (selection: HistorySelection) => {
    if (selection === 'chat')
      return (
        <ChatView
          ref={chatView}
          notice={<AiConnectionNotice />}
          chat={chat}
          composer={composer}
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
          initialPreview={handedPreview?.actionId === id ? handedPreview.preview : null}
          onAddProject={addProject}
        />
      );
    return (
      <SuggestionTaskPane
        key={id}
        suggestionId={id}
        title={title}
        requestedFile={requestedFile}
        canDismiss
        onStarted={(actionId, preview) => {
          select(`action:${actionId}`, { replace: true });
          if (preview) setHandedPreview({ from: selection, actionId, preview });
        }}
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
        todoNav={todoNav}
        selected={selection}
        onSelect={select}
        hidden={sidebarHidden}
      />
      {selection === null ? (
        <TodoScreen
          selection={todoSelection}
          week={week}
          settings={settings}
          settingsFailed={settings === null && workspace.errorMessage !== null}
          onSelect={selectTodos}
          onOpenWork={select}
          onAddProject={addProject}
        />
      ) : (
        <div className="history-detail">
          {/* A task or suggestion named in a task's sent message opens here, as the chat's do. */}
          <OpenWorkContext.Provider value={select}>
            {renderDetail(selection)}
          </OpenWorkContext.Provider>
        </div>
      )}
    </div>
  );
};

export default SuggestionHistoryPage;
