import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ChatItem, ChatItemPage } from '../../../electron/src/chat/chatContracts';
import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';
import type { OrchestrationStatus } from '../../../electron/src/orchestration/eventContracts';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import SuggestionHistoryPage from '@/pages/SuggestionHistoryPage';

const at = (minute: number) => `2026-10-08T01:${String(minute).padStart(2, '0')}:00.000Z`;

function item(sequence: number, content: ChatItem['content']): ChatItem {
  return { sequence, item_id: `item-${sequence}`, created_at: at(sequence), content };
}
const userMessage = (sequence: number, text: string, quote: string | null = null) =>
  item(sequence, { kind: 'user_message', text, quote_item_id: quote, images: [], files: [] });
const reply = (
  sequence: number,
  text: string,
  cards: { action_id: string; summary: string }[] = [],
  quote: string | null = null
) =>
  item(sequence, {
    kind: 'assistant_message',
    text,
    quote_item_id: quote,
    cards: cards.map((card) => ({ kind: 'action', ...card })),
  });

const work = (action_id: string, title: string, status: 'running' | 'idle') =>
  ({
    kind: 'conversation',
    action_id,
    title,
    updated_at: at(0),
    status,
    latest_completion_event_id: null,
  }) satisfies ConversationHistoryListItem;

let pages: ChatItemPage[];
let appendItem: (item: ChatItem) => void;
let publishStatus: (status: OrchestrationStatus) => void;
const listItems = vi.fn(async () => pages.shift() ?? { items: [], next_cursor: null });
const openConversation = vi.fn(async () => 'focused' as const);
const historyFetch = vi.fn(async () => ({
  data: [work('A1', '見積書のたたき台を作る', 'running')],
  nextCursor: null,
  unreadActionIds: [],
  error: null,
  errorCode: null,
}));

beforeEach(() => {
  localStorage.clear();
  window.electron = {
    chat: {
      listItems,
      sendMessage: vi.fn(),
      onItemAppended: (callback: (item: ChatItem) => void) => {
        appendItem = callback;
        return () => {};
      },
    },
    orchestration: {
      onStatus: (callback: (status: OrchestrationStatus) => void) => {
        publishStatus = callback;
        return () => {};
      },
    },
    history: { fetch: historyFetch, openConversation, openNewConversation: vi.fn() },
    process: { platform: 'darwin', env: { NODE_ENV: 'test' } },
  } as unknown as Window['electron'];
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderPage = () =>
  render(
    <UiLanguageProvider initialLanguage="ja">
      <SuggestionHistoryPage />
    </UiLanguageProvider>
  );

it('shows messages and cards, with a work’s status on its latest card only', async () => {
  pages = [
    {
      items: [
        item(7, { kind: 'turn_failure', reason: 'llm_connection' }),
        reply(6, '見積書のほうに伝えました。', [
          { action_id: 'A1', summary: '納期を直しています。' },
        ]),
        item(5, {
          kind: 'action_event',
          action_id: 'A1',
          event: 'completed',
          final_answer_excerpt: 'できました',
        }),
        userMessage(4, '納期を延ばして', 'item-2'),
        reply(3, '消えた作業です。', [{ action_id: 'GONE', summary: '削除された作業の要約' }]),
        reply(2, 'たたき台を作りますね。', [
          { action_id: 'A1', summary: '前回の見積もりから作ります。' },
        ]),
      ],
      next_cursor: null,
    },
  ];
  renderPage();

  const chat = await screen.findByRole('list', { name: 'Pantaray とのチャット' });
  expect(within(chat).getAllByRole('article', { name: 'Pantaray' })).toHaveLength(3);
  const mine = within(chat).getByRole('article', { name: 'あなた' });
  // The quoted reply is shown inside the user's bubble.
  expect(within(mine).getByText('たたき台を作りますね。')).toBeInTheDocument();
  expect(screen.queryByText('できました')).not.toBeInTheDocument();

  const cards = within(chat).getAllByRole('button', { name: '見積書のたたき台を作る を開く' });
  expect(cards).toHaveLength(2);
  await waitFor(() => expect(within(cards[1]).getByText('実行中')).toBeInTheDocument());
  expect(within(cards[0]).queryByText('実行中')).not.toBeInTheDocument();
  // A deleted work keeps its summary and shows no status.
  const gone = within(chat).getByRole('button', { name: '削除された作業の要約 を開く' });
  expect(gone).toHaveTextContent('削除された作業の要約');
  expect(within(gone).queryByText('実行中')).not.toBeInTheDocument();

  expect(screen.getByText('AI に接続できず、返信できませんでした。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'もう一度' })).not.toBeInTheDocument();
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();
});

it('merges live items once and reads the newest page again when the session starts over', async () => {
  pages = [{ items: [userMessage(1, 'おはよう')], next_cursor: null }];
  renderPage();
  await screen.findByText('おはよう');

  act(() => {
    appendItem(reply(2, 'おはようございます。'));
    appendItem(reply(2, 'おはようございます。'));
  });
  expect(screen.getAllByText('おはようございます。')).toHaveLength(1);
  expect(screen.getByRole('status', { name: '' })).toHaveTextContent(
    'Pantaray：おはようございます。'
  );

  pages = [{ items: [reply(3, '見逃した返信'), reply(2, 'おはようございます。')], next_cursor: 1 }];
  act(() => publishStatus({ status: 'session_started' }));
  await screen.findByText('見逃した返信');
  expect(listItems).toHaveBeenLastCalledWith({ before: null, limit: 50 });
  expect(screen.getAllByText('おはようございます。')).toHaveLength(1);
});

it('opens a card’s work in the Overlay and outlines it', async () => {
  pages = [
    { items: [reply(1, '始めます。', [{ action_id: 'A1', summary: '要約' }])], next_cursor: null },
  ];
  renderPage();
  const card = await screen.findByRole('button', { name: '見積書のたたき台を作る を開く' });
  card.focus();
  await userEvent.keyboard('{Enter}');
  expect(openConversation).toHaveBeenCalledWith({ actionId: 'A1' });
  expect(card).toHaveAttribute('aria-current', 'true');
});

it('remembers the chosen view and keeps focus on the switch', async () => {
  pages = [{ items: [], next_cursor: null }];
  const { unmount } = renderPage();
  const chatMode = await screen.findByRole('button', { name: 'チャット', pressed: true });
  expect(screen.getByRole('button', { name: '新しい作業' })).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: '一覧', pressed: false }));
  const listMode = screen.getByRole('button', { name: '一覧', pressed: true });
  expect(listMode).toHaveFocus();
  expect(screen.getByRole('searchbox')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '新しい作業' })).toBeInTheDocument();
  expect(chatMode).not.toBeInTheDocument();

  unmount();
  renderPage();
  expect(await screen.findByRole('button', { name: '一覧', pressed: true })).toBeInTheDocument();
});

it('stops loading older pages on its own after a failure until the reader asks again', async () => {
  // The older-page button is always in view here, as on a chat shorter than the window.
  const observed: (() => void)[] = [];
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      private connected = true;
      constructor(private readonly callback: IntersectionObserverCallback) {}
      observe() {
        const fire = () => {
          if (!this.connected) return;
          this.callback(
            [{ isIntersecting: true } as IntersectionObserverEntry],
            this as unknown as IntersectionObserver
          );
        };
        observed.push(fire);
        fire();
      }
      disconnect() {
        this.connected = false;
      }
    }
  );
  pages = [{ items: [userMessage(5, '最新')], next_cursor: 5 }];
  listItems.mockImplementationOnce(async () => pages.shift()!);
  listItems.mockRejectedValueOnce(new Error('offline'));
  try {
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('チャットを読み込めませんでした。');
    await act(async () => observed.forEach((fire) => fire()));
    expect(listItems).toHaveBeenCalledTimes(2);

    listItems.mockResolvedValueOnce({ items: [userMessage(4, '以前')], next_cursor: null });
    await userEvent.click(screen.getByRole('button', { name: '以前のメッセージを読み込む' }));
    expect(await screen.findByText('以前')).toBeInTheDocument();
    expect(listItems).toHaveBeenLastCalledWith({ before: 5, limit: 50 });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  } finally {
    vi.unstubAllGlobals();
  }
});
