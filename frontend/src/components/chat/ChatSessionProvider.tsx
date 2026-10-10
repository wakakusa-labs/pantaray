import { type ReactNode } from 'react';

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
  return (
    <ChatSessionContext.Provider value={{ chat, composer }}>{children}</ChatSessionContext.Provider>
  );
}
