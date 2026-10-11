import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { ChatView, type ChatViewHandle } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import { HistorySidebar } from '@/components/history/HistorySidebar';
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
import { useSuggestionHistory } from '@/hooks/useSuggestionHistory';

import './suggestionHistoryPage.css';

// A task the loaded list does not hold yet (still loading, filtered out, or older than its pages).
const UNLISTED_TITLE = {
  en: { action: 'Task', suggestion: 'Suggestion' },
  ja: { action: '作業', suggestion: '提案' },
} as const;

/**
 * History: the sidebar of the chat and the user's tasks, and the detail pane of the one selected.
 * The selection is the URL's `?item=`, so it survives a reload and the back button returns to it.
 */
const SuggestionHistoryPage = () => {
  const { chat, composer } = useChatSession();
  const history = useSuggestionHistory();
  const { language, t } = useI18n();
  const workspace = useWorkspaceSettingsController(t);
  const location = useLocation();
  const navigate = useNavigate();
  const selection = parseHistorySelection(location.search);
  const chatView = useRef<ChatViewHandle>(null);

  const select = (next: HistorySelection, options?: { replace: true }) => {
    // The chat already shown goes back to its newest message instead.
    if (next === 'chat' && selection === 'chat') {
      chatView.current?.showNewest();
      return;
    }
    navigate({ search: historySelectionSearch(next) }, options);
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

  const renderDetail = () => {
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
      return <TaskWorkspace key={id} actionId={id} title={title} onAddProject={addProject} />;
    return (
      <SuggestionTaskPane
        key={id}
        suggestionId={id}
        title={title}
        onStarted={(actionId) => select(`action:${actionId}`, { replace: true })}
        onAddProject={addProject}
      />
    );
  };

  return (
    <div className="history-page">
      <HistorySidebar
        history={history}
        projects={workspace}
        creatingProject={creatingProject}
        onCreatingProjectChange={setCreatingProject}
        selected={selection}
        onSelect={select}
      />
      <div className="history-detail">{renderDetail()}</div>
    </div>
  );
};

export default SuggestionHistoryPage;
