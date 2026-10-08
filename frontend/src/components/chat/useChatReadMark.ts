import { useCallback, useEffect } from 'react';

import type { ChatItem } from '../../../electron/src/chat/chatContracts';
import { useLocalOwner } from '@/context/localOwnerContext';

import { saveChatReadSequence } from './chatUnread';

/**
 * Marks the chat read up to its newest item while the reader can see that item: the window is
 * visible and the chat is scrolled to the bottom. Returns the check for the scroll handler.
 */
export function useChatReadMark(items: readonly ChatItem[], isFollowing: () => boolean) {
  const owner = useLocalOwner();
  const markRead = useCallback(() => {
    const newest = items[items.length - 1];
    if (!newest || document.visibilityState !== 'visible' || !isFollowing()) return;
    saveChatReadSequence(owner, newest.sequence);
  }, [items, isFollowing, owner]);

  useEffect(() => {
    markRead();
    document.addEventListener('visibilitychange', markRead);
    return () => document.removeEventListener('visibilitychange', markRead);
  }, [markRead]);

  return markRead;
}
