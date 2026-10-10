import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactNode } from 'react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type {
  ChatItem,
  ChatItemPage,
  ChatMessageRequest,
  ChatMessageSendResult,
} from '../../../electron/src/chat/chatContracts';
import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';
import type { OrchestrationStatus } from '../../../electron/src/orchestration/eventContracts';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { showChatState } from '@/history/historyViewMode';
import SuggestionHistoryPage from '@/pages/SuggestionHistoryPage';
import { ChatSessionProvider } from './ChatSessionProvider';
import { ChatUnreadTracker } from './ChatUnreadTracker';
import { ChatUnreadContext } from './chatUnread';

// The AI-connection notice reads the account; its own tests cover it.
vi.mock('@/components/AiConnectionNotice', () => ({ AiConnectionNotice: () => null }));

const at = (minute: number) => `2026-10-08T01:${String(minute).padStart(2, '0')}:00.000Z`;

function item(sequence: number, content: ChatItem['content']): ChatItem {
  return { sequence, item_id: `item-${sequence}`, created_at: at(sequence), content };
}
const userMessage = (sequence: number, text: string, quote: string | null = null) =>
  item(sequence, {
    kind: 'user_message',
    text,
    quote_item_id: quote,
    images: [],
    files: [],
    project_refs: [],
  });
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
let publishTurnState: (state: { running: boolean }) => void;
const retryTurn =
  vi.fn<(request: { failure_item_id: string }) => Promise<{ kind: 'started' | 'stale' }>>();
const listItems = vi.fn(async () => pages.shift() ?? { items: [], next_cursor: null });
const getTurnState = vi.fn(async (): Promise<{ running: boolean } | null> => null);
const openConversation = vi.fn(async () => 'focused' as const);
const sendMessage = vi.fn<(request: ChatMessageRequest) => Promise<ChatMessageSendResult>>();
const attachFile = vi.fn(async ({ name }: { name: string }) => ({
  attachmentId: '00000000-0000-4000-8000-000000000001',
  name,
  byteSize: 15,
}));
const discardAttachment = vi.fn(async () => undefined);
const historyFetch = vi.fn(async () => ({
  data: [work('A1', '見積書のたたき台を作る', 'running')] as ConversationHistoryListItem[],
  nextCursor: null,
  unreadActionIds: [],
  error: null,
  errorCode: null,
}));

beforeEach(() => {
  localStorage.clear();
  // Every subscriber hears an appended item, as main's relay reaches them all.
  const appended = new Set<(item: ChatItem) => void>();
  appendItem = (item) => appended.forEach((callback) => callback(item));
  window.electron = {
    chat: {
      listItems,
      sendMessage,
      retryTurn,
      getTurnState,
      onTurnState: (callback: (state: { running: boolean }) => void) => {
        publishTurnState = callback;
        return () => {};
      },
      onItemAppended: (callback: (item: ChatItem) => void) => {
        appended.add(callback);
        return () => appended.delete(callback);
      },
    },
    orchestration: {
      onStatus: (callback: (status: OrchestrationStatus) => void) => {
        publishStatus = callback;
        return () => {};
      },
    },
    history: { fetch: historyFetch, openConversation, openNewConversation: vi.fn() },
    actions: { attachFile, attachImage: vi.fn(), discardAttachment },
    process: { platform: 'darwin', env: { NODE_ENV: 'test' } },
  } as unknown as Window['electron'];
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function WorkspaceStub() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate('/history')}>
      Back to History
    </button>
  );
}

const OWNER = { id: 'user-1', kind: 'account' } as const;

const renderPage = (entry: { pathname: string; state?: unknown } = { pathname: '/history' }) =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <UiLanguageProvider initialLanguage="ja">
        <LocalOwnerContext.Provider value={OWNER}>
          <ChatSessionProvider>
            <Routes>
              <Route path="/history" element={<SuggestionHistoryPage />} />
              <Route path="/workspace" element={<WorkspaceStub />} />
            </Routes>
          </ChatSessionProvider>
        </LocalOwnerContext.Provider>
      </UiLanguageProvider>
    </MemoryRouter>
  );

it('labels a suggestion’s card as a suggestion, as the task list does', async () => {
  historyFetch.mockResolvedValueOnce({
    data: [
      {
        kind: 'suggestion',
        suggestion_id: 'S1',
        title: '請求書の下書きを作る',
        updated_at: at(0),
        status: 'approval_pending',
      },
    ],
    nextCursor: null,
    unreadActionIds: [],
    error: null,
    errorCode: null,
  });
  pages = [
    {
      items: [
        item(1, {
          kind: 'assistant_message',
          text: 'まだ答えていない提案があります。',
          quote_item_id: null,
          cards: [{ kind: 'suggestion', suggestion_id: 'S1', summary: '請求書の下書き' }],
        }),
      ],
      next_cursor: null,
    },
  ];
  renderPage();

  const card = await screen.findByRole('button', { name: '請求書の下書きを作る を開く' });
  await waitFor(() => expect(within(card).getByText('提案')).toBeInTheDocument());
  expect(within(card).queryByText('確認待ち')).not.toBeInTheDocument();
});

it('shows no quote on a reply that quoted a task event', async () => {
  pages = [
    {
      items: [
        reply(2, '調査結果を受け取りました。', [], 'item-1'),
        item(1, {
          kind: 'action_event',
          action_id: 'A1',
          event: 'completed',
          final_answer_excerpt: 'できました',
        }),
      ],
      next_cursor: null,
    },
  ];
  renderPage();

  const answer = await screen.findByRole('article', { name: 'Pantaray' });
  expect(within(answer).getByText('調査結果を受け取りました。')).toBeInTheDocument();
  expect(within(answer).queryByText('以前のメッセージ')).not.toBeInTheDocument();
  expect(
    within(answer).queryByRole('button', { name: '引用元のメッセージへ移動' })
  ).not.toBeInTheDocument();
});

it('shows messages and cards, with a work’s status on its latest card only', async () => {
  pages = [
    {
      items: [
        item(7, { kind: 'turn_failure', reason: 'llm_connection' }),
        reply(6, '納期を直しますね。', [{ action_id: 'A1', summary: '納期を直しています。' }]),
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

  // A failed turn has no notice of its own: a retry icon under the user's last message names why.
  expect(screen.queryByText('AI に接続できず、返信できませんでした。')).not.toBeInTheDocument();
  expect(
    within(mine).getByRole('button', {
      name: 'AI に接続できず、返信できませんでした。もう一度送る',
    })
  ).toHaveAttribute('title', 'AI に接続できず、返信できませんでした。もう一度送る');
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

it('shows the chat in the pane beside the sidebar of the chat row and the tasks', async () => {
  pages = [{ items: [reply(1, '始めます。')], next_cursor: null }];
  renderPage();
  const sidebar = screen.getByRole('complementary', { name: 'チャットと作業' });
  const chatRow = within(sidebar).getByRole('button', { name: 'チャット' });
  expect(chatRow).toHaveAttribute('aria-current', 'true');
  expect(within(sidebar).getByRole('button', { name: '新しい作業' })).toBeInTheDocument();
  expect(within(sidebar).getByRole('searchbox', { name: '作業を検索' })).toBeInTheDocument();
  expect(
    await within(sidebar).findByRole('button', { name: /^見積書のたたき台を作る/ })
  ).toHaveTextContent('実行中');

  const chat = await screen.findByRole('list', { name: 'Pantaray とのチャット' });
  expect(sidebar).not.toContainElement(chat);
  expect(screen.getByRole('heading', { level: 1, name: 'チャット' })).toBeInTheDocument();
  expect(within(chat).getByText('始めます。')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: 'メッセージ' })).toBeInTheDocument();
});

/** The unread count as `Layout` provides it, so the sidebar's Chat row shows it. */
function WithUnreadCount({ children }: { children: ReactNode }) {
  const [count, setCount] = useState(0);
  return (
    <ChatUnreadContext.Provider value={count}>
      <ChatUnreadTracker onCount={setCount} />
      {children}
    </ChatUnreadContext.Provider>
  );
}

it('the Chat row takes a reader who scrolled up back to the newest message, which reads it', async () => {
  const page = {
    items: [reply(2, 'おはようございます。'), userMessage(1, 'おはよう')],
    next_cursor: null,
  };
  // One read for the chat, one for the unread count.
  pages = [page, page];
  render(
    <MemoryRouter initialEntries={['/history']}>
      <UiLanguageProvider initialLanguage="ja">
        <LocalOwnerContext.Provider value={OWNER}>
          <ChatSessionProvider>
            <WithUnreadCount>
              <SuggestionHistoryPage />
            </WithUnreadCount>
          </ChatSessionProvider>
        </LocalOwnerContext.Provider>
      </UiLanguageProvider>
    </MemoryRouter>
  );
  await screen.findByText('おはようございます。');
  const scroll = document.querySelector<HTMLDivElement>('.chat-scroll')!;
  Object.defineProperties(scroll, {
    scrollHeight: { value: 1000, configurable: true },
    clientHeight: { value: 200 },
  });
  scroll.scrollTop = 100;
  fireEvent.scroll(scroll);

  act(() => appendItem(reply(3, '表紙も作りました。')));
  const chatRow = screen.getByRole('button', { name: 'チャット' });
  await waitFor(() => expect(chatRow).toHaveAccessibleDescription('未読 1 件'));
  expect(scroll.scrollTop).toBe(100);

  await userEvent.click(chatRow);
  expect(scroll.scrollTop).toBe(1000);
  await waitFor(() => expect(chatRow).not.toHaveAccessibleDescription());
  expect(chatRow).toHaveTextContent(/^チャット$/);
  expect(localStorage.getItem('pantaray.chat-read:account:user-1')).toBe('3');
});

it('the Chat row keeps the chat at the newest message when an older page lands after it', async () => {
  pages = [{ items: [reply(5, '最新の返信')], next_cursor: 5 }];
  renderPage();
  await screen.findByText('最新の返信');
  const scroll = document.querySelector<HTMLDivElement>('.chat-scroll')!;
  Object.defineProperties(scroll, {
    scrollHeight: { value: 1000, configurable: true },
    clientHeight: { value: 200 },
  });
  scroll.scrollTop = 100;
  fireEvent.scroll(scroll);
  let landOlder!: (page: ChatItemPage) => void;
  listItems.mockImplementationOnce(() => new Promise((resolve) => (landOlder = resolve)));
  await userEvent.click(screen.getByRole('button', { name: '以前のメッセージを読み込む' }));

  await userEvent.click(screen.getByRole('button', { name: 'チャット' }));
  expect(scroll.scrollTop).toBe(1000);
  await act(async () => landOlder({ items: [userMessage(4, '以前')], next_cursor: null }));
  expect(screen.getByText('以前')).toBeInTheDocument();
  expect(scroll.scrollTop).toBe(1000);

  // Still following: a reply that arrives is at the bottom, so it is read.
  act(() => appendItem(reply(6, '次の返信')));
  expect(localStorage.getItem('pantaray.chat-read:account:user-1')).toBe('6');
});

it('the Chat row drops an Overlay request still reading older pages for its card', async () => {
  pages = [{ items: [userMessage(3, 'ほかの話')], next_cursor: 3 }];
  let landOlder!: (page: ChatItemPage) => void;
  listItems.mockImplementationOnce(async () => pages.shift()!);
  listItems.mockImplementationOnce(() => new Promise((resolve) => (landOlder = resolve)));
  renderPage({ pathname: '/history', state: showChatState('A1') });
  await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ before: 3, limit: 50 }));

  const chatRow = screen.getByRole('button', { name: 'チャット' });
  await userEvent.click(chatRow);
  await act(async () =>
    landOlder({
      items: [reply(1, '始めます。', [{ action_id: 'A1', summary: '最初のカード' }])],
      next_cursor: null,
    })
  );
  const card = screen.getByRole('button', { name: '見積書のたたき台を作る を開く' });
  expect(card).not.toHaveFocus();
  expect(chatRow).toHaveFocus();
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

it('sends a quoted reply with a document once, and shows it as the user’s message', async () => {
  pages = [{ items: [reply(1, '資料を作りましょうか？')], next_cursor: null }];
  renderPage();
  const pantaray = await screen.findByRole('article', { name: 'Pantaray' });
  await userEvent.click(within(pantaray).getByRole('button', { name: '引用して返信' }));
  const input = screen.getByRole('textbox', { name: 'メッセージ' });
  expect(input).toHaveFocus();
  expect(screen.getByRole('button', { name: '引用をやめる' })).toBeInTheDocument();

  const file = new File(['%PDF-1.4\n%%EOF\n'], '議事録.pdf', { type: 'application/pdf' });
  await userEvent.upload(
    screen
      .getByRole('form', { name: 'Pantaray へのメッセージ' })
      .querySelector('input[type="file"]')!,
    file
  );
  expect(await screen.findByRole('button', { name: '議事録.pdf を削除' })).toBeInTheDocument();

  sendMessage.mockImplementationOnce(async (request) => ({
    kind: 'sent',
    item: {
      sequence: 2,
      item_id: 'item-2',
      created_at: at(2),
      content: {
        kind: 'user_message',
        text: request.text,
        quote_item_id: request.quote_item_id,
        images: request.images,
        files: request.files,
        project_refs: request.project_refs,
      },
    },
  }));
  // IME composition: Enter confirms the conversion and must not send.
  await userEvent.type(input, 'お願い');
  input.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: true })
  );
  expect(sendMessage).not.toHaveBeenCalled();
  await userEvent.keyboard('{Enter}');

  expect(sendMessage).toHaveBeenCalledOnce();
  const [request] = sendMessage.mock.calls[0];
  expect(request).toMatchObject({
    text: 'お願い',
    quote_item_id: 'item-1',
    images: [],
    files: [{ name: '議事録.pdf', byte_size: 15 }],
  });
  const mine = await screen.findByRole('article', { name: 'あなた' });
  expect(within(mine).getByText('資料を作りましょうか？')).toBeInTheDocument();
  expect(within(mine).getByRole('list', { name: '添付ファイル 1 件' })).toHaveTextContent(
    '議事録.pdf'
  );
  expect(input).toHaveValue('');
  expect(screen.queryByRole('button', { name: '引用をやめる' })).not.toBeInTheDocument();
  // A sent document is the backend's now; only unsent ones are discarded.
  expect(discardAttachment).not.toHaveBeenCalled();
});

it('resends a failed message with the same message_id and points at a refused field', async () => {
  pages = [{ items: [], next_cursor: null }];
  renderPage();
  const input = await screen.findByRole('textbox', { name: 'メッセージ' });
  sendMessage.mockRejectedValueOnce(new Error('offline'));
  await userEvent.type(input, 'こんにちは{Enter}');
  expect(await screen.findByRole('alert')).toHaveTextContent('メッセージを送れませんでした。');
  expect(input).toHaveAttribute('readonly');

  sendMessage.mockResolvedValueOnce({ kind: 'rejected', field: 'text' });
  await userEvent.click(screen.getByRole('button', { name: '同じメッセージを再送' }));
  expect(sendMessage).toHaveBeenCalledTimes(2);
  expect(sendMessage.mock.calls[1][0].message_id).toBe(sendMessage.mock.calls[0][0].message_id);
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '有効なメッセージを入力してください。'
  );
  expect(input).not.toHaveAttribute('readonly');
  expect(input).toHaveAttribute('aria-invalid', 'true');
});

it('opens on the chat at the Action’s latest card when the Overlay asks, reading older pages', async () => {
  pages = [
    { items: [userMessage(3, 'ほかの話'), userMessage(2, 'まだほかの話')], next_cursor: 2 },
    {
      items: [reply(1, '始めます。', [{ action_id: 'A1', summary: '最初のカード' }])],
      next_cursor: null,
    },
  ];
  renderPage({ pathname: '/history', state: showChatState('A1') });
  const card = await screen.findByRole('button', { name: '見積書のたたき台を作る を開く' });
  await waitFor(() => expect(card).toHaveFocus());
  expect(card).toHaveAttribute('aria-current', 'true');
  expect(listItems).toHaveBeenLastCalledWith({ before: 2, limit: 50 });
});

it('discards a document whose write finishes after the page closed', async () => {
  pages = [{ items: [], next_cursor: null }];
  let finishWrite: () => void = () => {};
  attachFile.mockImplementationOnce(
    ({ name }) =>
      new Promise((resolve) => {
        finishWrite = () =>
          resolve({ attachmentId: '00000000-0000-4000-8000-000000000002', name, byteSize: 15 });
      })
  );
  const { unmount } = renderPage();
  await screen.findByRole('textbox', { name: 'メッセージ' });
  await userEvent.upload(
    screen
      .getByRole('form', { name: 'Pantaray へのメッセージ' })
      .querySelector('input[type="file"]')!,
    new File(['%PDF-1.4\n'], '遅い.pdf', { type: 'application/pdf' })
  );
  unmount();
  await act(async () => finishWrite());
  await waitFor(() =>
    expect(discardAttachment).toHaveBeenCalledWith({
      attachmentId: '00000000-0000-4000-8000-000000000002',
    })
  );
});

it('does not read an older page with the old cursor while the newest page is read again', async () => {
  pages = [{ items: [userMessage(5, '最新')], next_cursor: 5 }];
  renderPage();
  await screen.findByText('最新');
  let finishReload: (page: ChatItemPage) => void = () => {};
  listItems.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishReload = resolve;
      })
  );
  act(() => publishStatus({ status: 'session_started' }));
  await userEvent.click(screen.getByRole('button', { name: '以前のメッセージを読み込む' }));
  expect(listItems).not.toHaveBeenCalledWith({ before: 5, limit: 50 });

  await act(async () => finishReload({ items: [userMessage(9, '再接続後')], next_cursor: 9 }));
  await screen.findByText('再接続後');
  listItems.mockResolvedValueOnce({ items: [userMessage(8, 'ひとつ前')], next_cursor: null });
  await userEvent.click(screen.getByRole('button', { name: '以前のメッセージを読み込む' }));
  expect(listItems).toHaveBeenLastCalledWith({ before: 9, limit: 50 });
  expect(await screen.findByText('ひとつ前')).toBeInTheDocument();
});

it('shows the typing bubble while a turn runs, and clears it when a new session starts', async () => {
  pages = [{ items: [userMessage(1, 'こんにちは')], next_cursor: null }];
  renderPage();
  await screen.findByText('こんにちは');
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();

  act(() => publishTurnState({ running: true }));
  expect(screen.getByRole('status', { name: '入力中' })).toBeInTheDocument();
  act(() => publishTurnState({ running: false }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();

  act(() => publishTurnState({ running: true }));
  pages = [{ items: [userMessage(1, 'こんにちは')], next_cursor: null }];
  act(() => publishStatus({ status: 'session_started' }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();
});

it('runs the newest failed turn again, and reads the chat again when another turn ended since', async () => {
  pages = [
    {
      items: [
        item(3, { kind: 'turn_failure', reason: 'llm_request' }),
        userMessage(2, 'もう一回'),
        item(1, { kind: 'turn_failure', reason: 'llm_connection' }),
      ],
      next_cursor: null,
    },
  ];
  renderPage();
  const retryName = 'AI への依頼が失敗し、返信できませんでした。もう一度送る';
  const lastMessage = await screen.findByRole('article', { name: 'あなた' });
  // Only the newest failure offers it, under the user's last message.
  expect(screen.getAllByRole('button', { name: /もう一度送る/ })).toHaveLength(1);
  expect(within(lastMessage).getByRole('button', { name: retryName })).toBeInTheDocument();

  act(() => publishTurnState({ running: true }));
  expect(screen.queryByRole('button', { name: /もう一度送る/ })).not.toBeInTheDocument();
  act(() => publishTurnState({ running: false }));

  retryTurn.mockResolvedValueOnce({ kind: 'started' });
  screen.getByRole('button', { name: retryName }).focus();
  await userEvent.keyboard('{Enter}');
  expect(retryTurn).toHaveBeenCalledWith({ failure_item_id: 'item-3' });
  expect(listItems).toHaveBeenCalledTimes(1);

  retryTurn.mockResolvedValueOnce({ kind: 'stale' });
  pages = [
    {
      items: [reply(4, '答えました。'), item(3, { kind: 'turn_failure', reason: 'llm_request' })],
      next_cursor: null,
    },
  ];
  await userEvent.click(screen.getByRole('button', { name: retryName }));
  expect(await screen.findByText('答えました。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /もう一度送る/ })).not.toBeInTheDocument();
});

it('a page loaded mid-turn shows the typing bubble from the state main last heard', async () => {
  pages = [{ items: [userMessage(1, '調べておいて')], next_cursor: null }];
  getTurnState.mockResolvedValueOnce({ running: true });
  renderPage();
  expect(await screen.findByRole('status', { name: '入力中' })).toBeInTheDocument();
  act(() => publishTurnState({ running: false }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();
});

it('a turn change heard before the read of the state arrives wins over it', async () => {
  pages = [{ items: [userMessage(1, '調べておいて')], next_cursor: null }];
  let answer: (state: { running: boolean }) => void = () => {};
  getTurnState.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      })
  );
  renderPage();
  await screen.findByText('調べておいて');
  act(() => publishTurnState({ running: false }));
  await act(async () => answer({ running: true }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();
});

it('a closed session clears the typing bubble, so a failed turn can be retried', async () => {
  pages = [{ items: [userMessage(1, '調べておいて')], next_cursor: null }];
  let answer: (state: { running: boolean }) => void = () => {};
  getTurnState.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      })
  );
  renderPage();
  await screen.findByText('調べておいて');
  act(() => publishTurnState({ running: true }));
  act(() => publishStatus({ status: 'closed' }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();
  // The read started at mount answers late with what main knew then.
  await act(async () => answer({ running: true }));
  expect(screen.queryByRole('status', { name: '入力中' })).not.toBeInTheDocument();

  pages = [
    {
      items: [
        item(2, { kind: 'turn_failure', reason: 'llm_connection' }),
        userMessage(1, '調べておいて'),
      ],
      next_cursor: null,
    },
  ];
  // The session that starts again reads the chat again.
  act(() => publishStatus({ status: 'session_started' }));
  expect(
    await screen.findByRole('button', {
      name: 'AI に接続できず、返信できませんでした。もう一度送る',
    })
  ).toBeInTheDocument();
});

it('reads older pages to find the user message a failed turn’s retry goes under', async () => {
  pages = [
    {
      items: [item(60, { kind: 'turn_failure', reason: 'internal' }), reply(59, '途中経過です。')],
      next_cursor: 59,
    },
    { items: [userMessage(10, '資料をまとめて')], next_cursor: null },
  ];
  renderPage();
  const message = await screen.findByRole('article', { name: 'あなた' });
  expect(
    within(message).getByRole('button', { name: '返信できませんでした。もう一度送る' })
  ).toBeInTheDocument();
  expect(listItems).toHaveBeenLastCalledWith({ before: 59, limit: 50 });
});

it('names a workspace project with @ and sends it the way an Action message names it', async () => {
  // prettier-ignore
  const workspace = { read_access_scope: 'workspace', organizations: [], projects: [{ project_id: 'p-1', display_name: 'Aurora Web', sort_order: 0, organization_ids: [] }], folders: [{ folder_id: 'f-1', display_name: 'aurora', real_path: '/Users/me/aurora', canonical_real_path: '/Users/me/aurora', organization_ids: [], project_ids: ['p-1'] }] };
  (window.electron as unknown as { workspaceSettings: unknown }).workspaceSettings = {
    get: async () => workspace,
  };
  pages = [{ items: [], next_cursor: null }];
  renderPage();
  const input = (await screen.findByRole('textbox', { name: 'メッセージ' })) as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: '🙂 @Au', selectionStart: 6 } });
  expect(await screen.findByRole('option', { name: 'Aurora Web' })).toBeInTheDocument();
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(input.value).toBe('🙂 Aurora Web ');
  fireEvent.change(input, {
    target: { value: '🙂 Aurora Web の README を要約して', selectionStart: 28 },
  });

  sendMessage.mockImplementationOnce(async (request) => ({
    kind: 'sent',
    item: {
      sequence: 1,
      item_id: 'item-1',
      created_at: at(1),
      content: {
        kind: 'user_message',
        text: request.text,
        quote_item_id: null,
        images: [],
        files: [],
        project_refs: request.project_refs,
      },
    },
  }));
  fireEvent.keyDown(input, { key: 'Enter' });

  // The emoji is one code point: the span counts code points, not UTF-16 units.
  expect(sendMessage.mock.calls[0][0].project_refs).toEqual([
    {
      project_id: 'p-1',
      display_name: 'Aurora Web',
      paths: ['/Users/me/aurora'],
      start: 2,
      end: 12,
    },
  ]);
  const mine = await screen.findByRole('article', { name: 'あなた' });
  expect(within(mine).getByText('Aurora Web')).toHaveClass('action-conversation__project-ref');
});

it('keeps the draft and its document through a trip to Workspace to add a project', async () => {
  (window.electron as unknown as { workspaceSettings: unknown }).workspaceSettings = {
    get: async () => ({
      read_access_scope: 'workspace',
      organizations: [],
      projects: [],
      folders: [],
    }),
  };
  pages = [{ items: [], next_cursor: null }];
  renderPage();
  const input = (await screen.findByRole('textbox', { name: 'メッセージ' })) as HTMLTextAreaElement;
  await userEvent.upload(
    screen
      .getByRole('form', { name: 'Pantaray へのメッセージ' })
      .querySelector('input[type="file"]')!,
    new File(['%PDF-1.4\n'], '議事録.pdf', { type: 'application/pdf' })
  );
  await screen.findByRole('button', { name: '議事録.pdf を削除' });
  fireEvent.change(input, { target: { value: 'これを @', selectionStart: 5 } });
  await userEvent.click(await screen.findByRole('option', { name: 'プロジェクトを追加' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Back to History' }));

  // The draft, @ included, and the staged file are as they were left.
  expect(await screen.findByRole('textbox', { name: 'メッセージ' })).toHaveValue('これを @');
  expect(screen.getByRole('button', { name: '議事録.pdf を削除' })).toBeInTheDocument();
  expect(discardAttachment).not.toHaveBeenCalled();
});

it('a reply that arrives before the chat is drawn is not marked read', async () => {
  listItems.mockImplementationOnce(() => new Promise(() => {}));
  renderPage();
  act(() => appendItem(reply(5, '下書きを直しました。')));
  expect(screen.queryByText('下書きを直しました。')).not.toBeInTheDocument();
  expect(localStorage.getItem('pantaray.chat-read:account:user-1')).toBeNull();
});

const sourceMessage = (text: string) =>
  screen
    .getAllByRole('article', { name: 'Pantaray' })
    .find((row) => row.textContent?.includes(text))!;

it('jumps from a quote to the quoted message, and back to the quote', async () => {
  pages = [
    {
      items: [
        userMessage(3, 'それで進めて', 'item-1'),
        reply(2, '納期はいつですか？'),
        reply(1, 'たたき台を作りますね。'),
      ],
      next_cursor: null,
    },
  ];
  renderPage();
  const mine = await screen.findByRole('article', { name: 'あなた' });
  await userEvent.click(within(mine).getByRole('button', { name: '引用元のメッセージへ移動' }));
  expect(sourceMessage('たたき台を作りますね。')).toHaveFocus();

  await userEvent.click(screen.getByRole('button', { name: '元のメッセージに戻る' }));
  expect(mine).toHaveFocus();
  expect(screen.queryByRole('button', { name: '元のメッセージに戻る' })).not.toBeInTheDocument();
});

it('reads older pages to jump to a quoted message that is not loaded yet', async () => {
  pages = [
    { items: [userMessage(50, 'それで進めて', 'item-1')], next_cursor: 50 },
    { items: [reply(30, '途中の話です。')], next_cursor: 30 },
    { items: [reply(1, 'たたき台を作りますね。')], next_cursor: null },
  ];
  renderPage();
  const mine = await screen.findByRole('article', { name: 'あなた' });
  expect(within(mine).getByText('以前のメッセージ')).toBeInTheDocument();
  expect(listItems).toHaveBeenCalledTimes(1);

  await userEvent.click(within(mine).getByRole('button', { name: '引用元のメッセージへ移動' }));
  await waitFor(() => expect(sourceMessage('たたき台を作りますね。')).toHaveFocus());
  expect(listItems).toHaveBeenLastCalledWith({ before: 30, limit: 50 });
  expect(within(mine).getByText('たたき台を作りますね。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '元のメッセージに戻る' })).toBeInTheDocument();
});

it('stays in place without a way back when an older page cannot be read', async () => {
  pages = [{ items: [userMessage(50, 'それで進めて', 'item-1')], next_cursor: 50 }];
  listItems.mockImplementationOnce(async () => pages.shift()!);
  listItems.mockRejectedValueOnce(new Error('offline'));
  renderPage();
  const mine = await screen.findByRole('article', { name: 'あなた' });
  const quote = within(mine).getByRole('button', { name: '引用元のメッセージへ移動' });
  await userEvent.click(quote);
  expect(await screen.findByRole('alert')).toHaveTextContent('チャットを読み込めませんでした。');
  expect(listItems).toHaveBeenCalledTimes(2);
  expect(quote).toHaveFocus();
  expect(screen.queryByRole('button', { name: '元のメッセージに戻る' })).not.toBeInTheDocument();
});

it('drops the way back to the quote once the user sends a message', async () => {
  pages = [
    {
      items: [userMessage(2, 'それで進めて', 'item-1'), reply(1, 'たたき台を作りますね。')],
      next_cursor: null,
    },
  ];
  renderPage();
  const mine = await screen.findByRole('article', { name: 'あなた' });
  await userEvent.click(within(mine).getByRole('button', { name: '引用元のメッセージへ移動' }));
  expect(screen.getByRole('button', { name: '元のメッセージに戻る' })).toBeInTheDocument();

  sendMessage.mockResolvedValueOnce({ kind: 'sent', item: userMessage(3, '追加でお願い') });
  await userEvent.type(screen.getByRole('textbox', { name: 'メッセージ' }), '追加でお願い{Enter}');
  expect(sendMessage).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: '元のメッセージに戻る' })).not.toBeInTheDocument();
});
