import { useEffect, useState, type ReactNode } from 'react';

import { createTaskComposerDrafts } from '@/components/task/taskComposerDrafts';
import { useChatItems } from '@/hooks/useChatItems';

import { ChatSessionContext } from './chatSession';
import { useChatComposer } from './useChatComposer';

/**
 * Holds the chat above the owner's pages, so a draft and its staged attachments survive a trip
 * to Workspace ("Add project" from the @ list) and back. The owner boundary still remounts it.
 */
export function ChatSessionProvider({ children }: { children: ReactNode }) {
  const chat = useChatItems();
  const composer = useChatComposer({ onSent: chat.appendItem });
  const [taskDrafts] = useState(() => createTaskComposerDrafts(window.electron?.actions));
  useEffect(() => {
    taskDrafts.open();
    return taskDrafts.close;
  }, [taskDrafts]);
  return (
    <ChatSessionContext.Provider value={{ chat, composer, taskDrafts }}>
      {children}
    </ChatSessionContext.Provider>
  );
}
