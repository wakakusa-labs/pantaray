import { createContext, useContext } from 'react';

import type { ChatItemsResult } from '@/hooks/useChatItems';

import type { ChatComposerControl } from './useChatComposer';

/** The owner's chat and its composer, kept while History and Workspace swap places. */
export type ChatSession = {
  chat: ChatItemsResult;
  composer: ChatComposerControl;
};

export const ChatSessionContext = createContext<ChatSession | null>(null);

export function useChatSession(): ChatSession {
  const session = useContext(ChatSessionContext);
  if (!session) throw new Error('useChatSession must be used within ChatSessionProvider');
  return session;
}
