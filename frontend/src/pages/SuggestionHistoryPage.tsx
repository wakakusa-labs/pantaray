import { useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { ChatView, type ChatViewHandle } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import { HistorySidebar } from '@/components/history/HistorySidebar';
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

/**
 * History: the sidebar of the chat and the user's tasks, and the detail pane of the one selected.
 * The selection is the URL's `?item=`, so it survives a reload and the back button returns to it.
 */
const SuggestionHistoryPage = () => {
  const { chat, composer, taskDrafts } = useChatSession();
  const history = useSuggestionHistory();
  const { language, t } = useI18n();
  const workspace = useWorkspaceSettingsController(t);
  const location = useLocation();
  const navigate = useNavigate();
  const selection = parseHistorySelection(location.search);
  // `Layout` and the task pane bring the page here with the Action to show in the chat.
  const reveal = readShowChatState(location.state, location.key);
  const chatView = useRef<ChatViewHandle>(null);

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
  const addProject = () => void workspace.addProjectFromFolder();

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
          drafts={taskDrafts}
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
        drafts={taskDrafts}
      />
    );
  };

  return (
    <div className="history-page">
      <HistorySidebar
        history={history}
        projects={workspace}
        selected={selection}
        onSelect={select}
      />
      <div className="history-detail">{renderDetail()}</div>
    </div>
  );
};

export default SuggestionHistoryPage;
