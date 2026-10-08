import { expect, it } from 'vitest';

import type { ChatItem } from '../../../electron/src/chat/chatContracts';
import { latestCardPositions, mergeChatItems, replaceWithNewestPage } from './chatTimeline';

const user = (sequence: number): ChatItem => ({
  sequence,
  item_id: `item-${sequence}`,
  created_at: '2026-10-08T01:00:00.000Z',
  content: {
    kind: 'user_message',
    text: `message ${sequence}`,
    quote_item_id: null,
    images: [],
    files: [],
  },
});

const reply = (sequence: number, actionIds: string[]): ChatItem => ({
  sequence,
  item_id: `item-${sequence}`,
  created_at: '2026-10-08T01:00:00.000Z',
  content: {
    kind: 'assistant_message',
    text: `reply ${sequence}`,
    quote_item_id: null,
    cards: actionIds.map((action_id) => ({ kind: 'action', action_id, summary: 'summary' })),
  },
});

const ids = (items: ChatItem[]) => items.map((item) => item.item_id);

it('keeps an item once and in chat order when a live append overlaps a page', () => {
  const merged = mergeChatItems([user(3), user(5)], [user(4), user(3), user(1)]);
  expect(ids(merged)).toEqual(['item-1', 'item-3', 'item-4', 'item-5']);
});

it('a reload keeps items appended after its page and drops older pages that may leave a gap', () => {
  const current = [user(1), user(2), user(9), user(12)];
  const page = [user(10), user(9), user(8)];
  expect(ids(replaceWithNewestPage(current, page))).toEqual([
    'item-8',
    'item-9',
    'item-10',
    'item-12',
  ]);
  expect(ids(replaceWithNewestPage(current, []))).toEqual(ids(current));
});

it('only the last card of each work is its latest', () => {
  const latest = latestCardPositions([
    reply(1, ['A']),
    user(2),
    reply(3, ['B', 'A']),
    reply(4, ['B']),
  ]);
  expect(latest.get('action:A')).toBe('item-3#1');
  expect(latest.get('action:B')).toBe('item-4#0');
});
