import { useLayoutEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { ChatView } from '@/components/chat/ChatView';
import { useChatSession } from '@/components/chat/chatSession';
import { HistoryModeSwitch } from '@/components/history/HistoryModeSwitch';
import { HistorySidebar } from '@/components/history/HistorySidebar';
import { useI18n } from '@/context/useI18n';
import {
  historyModeButtonId,
  readHistoryViewMode,
  readShowChatState,
  saveHistoryViewMode,
  type HistoryViewMode,
} from '@/history/historyViewMode';

import './suggestionHistoryPage.css';

/** History: the single chat by default, or the list; the viewer's choice is remembered. */
const SuggestionHistoryPage = () => {
  const { t } = useI18n();
  const [mode, setMode] = useState<HistoryViewMode>(readHistoryViewMode);
  const { chat, composer } = useChatSession();
  // `Layout` brings the page here with the Action the Overlay asked to show in the chat.
  const location = useLocation();
  const reveal = readShowChatState(location.state, location.key);
  const [revealSeen, setRevealSeen] = useState<string | null>(null);
  if (reveal && reveal.key !== revealSeen) {
    setRevealSeen(reveal.key);
    setMode('chat');
  }
  const switchedRef = useRef(false);
  // The switch is redrawn inside the other view's toolbar, so the pressed button gets focus back.
  useLayoutEffect(() => {
    if (!switchedRef.current) return;
    switchedRef.current = false;
    document.getElementById(historyModeButtonId(mode))?.focus();
  }, [mode]);
  const modeSwitch = (
    <HistoryModeSwitch
      mode={mode}
      t={t}
      onChange={(next) => {
        saveHistoryViewMode(next);
        switchedRef.current = true;
        setMode(next);
      }}
    />
  );
  return mode === 'list' ? (
    <HistorySidebar modeSwitch={modeSwitch} />
  ) : (
    <ChatView modeSwitch={modeSwitch} chat={chat} composer={composer} reveal={reveal} />
  );
};

export default SuggestionHistoryPage;
