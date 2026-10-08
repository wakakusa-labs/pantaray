import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ChatItem, ChatItemPage } from '../../../electron/src/chat/chatContracts';
import type { OrchestrationStatus } from '../../../electron/src/orchestration/eventContracts';
import { LocalOwnerContext } from '@/context/localOwnerContext';

import { ChatUnreadTracker } from './ChatUnreadTracker';

const OWNER = { id: 'user-1', kind: 'account' } as const;
const KEY = 'pantaray.chat-read:account:user-1';

const reply = (sequence: number): ChatItem => ({
  sequence,
  item_id: `item-${sequence}`,
  created_at: '2026-10-09T00:00:00.000Z',
  content: { kind: 'assistant_message', text: '返信', quote_item_id: null, cards: [] },
});

let page: ChatItemPage;
let append: (item: ChatItem) => void;
let publishStatus: (status: OrchestrationStatus) => void;

beforeEach(() => {
  localStorage.clear();
  page = { items: [], next_cursor: null };
  window.electron = {
    chat: {
      listItems: vi.fn(async () => page),
      onItemAppended: (callback: (item: ChatItem) => void) => {
        append = callback;
        return () => {};
      },
    },
    orchestration: {
      onStatus: (callback: (status: OrchestrationStatus) => void) => {
        publishStatus = callback;
        return () => {};
      },
    },
  } as unknown as Window['electron'];
});

afterEach(() => vi.clearAllMocks());

it('an empty chat starts read, so a reply after it stays unread through a reconnect', async () => {
  const counts: number[] = [];
  render(
    <LocalOwnerContext.Provider value={OWNER}>
      <ChatUnreadTracker onCount={(count) => counts.push(count)} />
    </LocalOwnerContext.Provider>
  );
  await act(async () => {});
  expect(localStorage.getItem(KEY)).toBe('0');

  act(() => append(reply(1)));
  expect(counts[counts.length - 1]).toBe(1);

  page = { items: [reply(1)], next_cursor: null };
  await act(async () => publishStatus({ status: 'session_started' } as OrchestrationStatus));
  expect(localStorage.getItem(KEY)).toBe('0');
  expect(counts[counts.length - 1]).toBe(1);
});
