import { useCallback, useEffect, useRef, useState } from 'react';

import type { ChatItem } from '../../electron/src/chat/chatContracts';
import { mergeChatItems, replaceWithNewestPage } from '@/components/chat/chatTimeline';

const CHAT_PAGE_SIZE = 50;

export type ChatItemsResult = {
  /** Oldest first. */
  items: ChatItem[];
  /** The last item that arrived live, for announcing it; pages read never count. */
  arrived: ChatItem | null;
  loading: boolean;
  failed: boolean;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => Promise<void>;
  reload: () => Promise<void>;
  /** Adds an item this window appended itself; the relay's copy of it is the same item. */
  appendItem: (item: ChatItem) => void;
};

/**
 * The owner's single chat, newest page first and older pages on demand.
 *
 * Live items arrive through `chat:itemAppended`; a WS session that started over may have missed
 * some, so `session_started` reads the newest page again. A reload bumps the generation, which
 * drops an older page still in flight: its cursor belongs to the chat as it was read before.
 */
export function useChatItems(): ChatItemsResult {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [arrived, setArrived] = useState<ChatItem | null>(null);
  const [olderCursor, setOlderCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const generationRef = useRef(0);
  const loadingOlderRef = useRef(false);

  const reload = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const chat = window.electron?.chat;
      if (!chat) throw new Error('Chat bridge is unavailable.');
      const page = await chat.listItems({ before: null, limit: CHAT_PAGE_SIZE });
      if (generation !== generationRef.current) return;
      setItems((current) => replaceWithNewestPage(current, page.items));
      setOlderCursor(page.next_cursor);
      setFailed(false);
    } catch {
      if (generation === generationRef.current) setFailed(true);
    } finally {
      if (generation === generationRef.current) setLoading(false);
    }
  }, []);

  const loadOlder = useCallback(async () => {
    const chat = window.electron?.chat;
    if (olderCursor === null || loadingOlderRef.current || !chat) return;
    const generation = generationRef.current;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      const page = await chat.listItems({ before: olderCursor, limit: CHAT_PAGE_SIZE });
      if (generation !== generationRef.current) return;
      setItems((current) => mergeChatItems(current, page.items));
      setOlderCursor(page.next_cursor);
      setFailed(false);
    } catch {
      if (generation === generationRef.current) setFailed(true);
    } finally {
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [olderCursor]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const onItemAppended = window.electron?.chat?.onItemAppended;
    if (!onItemAppended) return;
    return onItemAppended((item) => {
      setItems((current) => mergeChatItems(current, [item]));
      setArrived(item);
    });
  }, []);

  useEffect(() => {
    const onStatus = window.electron?.orchestration?.onStatus;
    if (!onStatus) return;
    return onStatus((status) => {
      if (status.status === 'session_started') void reload();
    });
  }, [reload]);

  return {
    items,
    arrived,
    loading,
    failed,
    hasOlder: olderCursor !== null,
    loadingOlder,
    loadOlder,
    reload,
    appendItem: useCallback(
      (item: ChatItem) => setItems((current) => mergeChatItems(current, [item])),
      []
    ),
  };
}
