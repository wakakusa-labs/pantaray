import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';

import type { ChatItem } from '../../../electron/src/chat/chatContracts';
import { useLocalOwner } from '@/context/localOwnerContext';

import {
  readChatReadSequence,
  saveChatReadSequence,
  subscribeChatReadSequence,
} from './chatUnread';

// Design limit: after a restart or reconnect only the newest 50 items are counted; read older
// pages when a count above that is reported as wrong.
const UNREAD_PAGE_SIZE = 50;

const pantarayMessageSequences = (items: readonly ChatItem[]) =>
  items.filter((item) => item.content.kind === 'assistant_message').map((item) => item.sequence);

/**
 * Counts Pantaray's chat messages newer than the viewer's read position, from the newest page
 * and the items that arrive live, and reports the count. It renders nothing.
 *
 * The first read with no stored position marks the chat read up to its newest item, so
 * messages from before the position existed never show as unread.
 */
export function ChatUnreadTracker({ onCount }: { onCount: (count: number) => void }) {
  const owner = useLocalOwner();
  const readSequence = useSyncExternalStore(subscribeChatReadSequence, () =>
    readChatReadSequence(owner)
  );
  const [sequences, setSequences] = useState<readonly number[]>([]);

  // Reads the newest page; the chat itself reports its own read failures, so a failed read
  // leaves the count as it was.
  const readNewestPage = useCallback(() => {
    const chat = window.electron?.chat;
    if (!chat) return;
    chat.listItems({ before: null, limit: UNREAD_PAGE_SIZE }).then(
      (page) => {
        if (readChatReadSequence(owner) === null) {
          saveChatReadSequence(owner, page.items[0]?.sequence ?? 0);
        }
        setSequences((current) => [
          ...new Set([...current, ...pantarayMessageSequences(page.items)]),
        ]);
      },
      (error: unknown) => console.warn('Chat unread count could not be read.', error)
    );
  }, [owner]);

  useEffect(readNewestPage, [readNewestPage]);

  useEffect(() => {
    const onItemAppended = window.electron?.chat?.onItemAppended;
    if (!onItemAppended) return;
    return onItemAppended((item) => {
      if (item.content.kind !== 'assistant_message') return;
      setSequences((current) =>
        current.includes(item.sequence) ? current : [...current, item.sequence]
      );
    });
  }, []);

  // A WS session that started over may have missed items, as the chat itself assumes.
  useEffect(() => {
    const onStatus = window.electron?.orchestration?.onStatus;
    if (!onStatus) return;
    return onStatus((status) => {
      if (status.status === 'session_started') readNewestPage();
    });
  }, [readNewestPage]);

  const count = sequences.filter((sequence) => sequence > (readSequence ?? 0)).length;
  useEffect(() => {
    onCount(count);
  }, [count, onCount]);
  useEffect(() => () => onCount(0), [onCount]);
  return null;
}
