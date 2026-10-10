import { useLocation } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { ChatView } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import { HistorySidebar } from '@/components/history/HistorySidebar';
import { readShowChatState } from '@/history/historyViewMode';

import './suggestionHistoryPage.css';

/** History: the sidebar of the chat and the user's tasks, and the chat beside it. */
const SuggestionHistoryPage = () => {
  const { chat, composer } = useChatSession();
  // `Layout` brings the page here with the Action the Overlay asked to show in the chat.
  const location = useLocation();
  const reveal = readShowChatState(location.state, location.key);
  return (
    <div className="history-page">
      <HistorySidebar />
      <div className="history-detail">
        <ChatView notice={<AiConnectionNotice />} chat={chat} composer={composer} reveal={reveal} />
      </div>
    </div>
  );
};

export default SuggestionHistoryPage;
