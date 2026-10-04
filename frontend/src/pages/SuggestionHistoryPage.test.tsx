import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ActionConversationPage } from '../../electron/src/actions/actionContracts';
import type {
  ActionLiveSnapshot,
  ActionLiveUpdate,
} from '../../electron/src/actions/actionLiveCore';
import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';

import { COMMON_MESSAGES } from '@/i18n/messageCatalog/common';
import { HISTORY_MESSAGES } from '@/i18n/messageCatalog/history';
import { t as translate } from '@/i18n/translate';
import SuggestionHistoryPage from './SuggestionHistoryPage';
const mocks = vi.hoisted(() => ({
  error: 'history.error.fetchFailed' as string | null,
  itemsOverride: null as ConversationHistoryListItem[] | null,
  loading: false,
  loadMore: vi.fn(),
  markCompletionViewed: vi.fn(async () => undefined),
  removeItem: vi.fn(),
  setSearchText: vi.fn(),
  unreadActionId: 'A1' as string | null,
}));
vi.mock('@/context/useI18n', async () => {
  const { formatDateTime } = await import('@/i18n/translate');
  return {
    useI18n: () => ({
      language: 'ja',
      t: (key: string) => key,
      formatDateTime: (date: Date) => formatDateTime('ja', date),
    }),
  };
});
vi.mock('@/hooks/useSuggestionHistory', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/hooks/useSuggestionHistory')>()),
  useSuggestionHistory: () => ({
    items: mocks.itemsOverride ?? [
      {
        kind: 'conversation',
        action_id: 'A1',
        title: 'Conversation',
        updated_at: '2026-08-30T01:02:03.000Z',
        status: 'idle',
        latest_completion_event_id: 'C1',
      },
      {
        kind: 'suggestion',
        suggestion_id: 'S1',
        title: 'Suggestion',
        updated_at: '2026-08-30T01:02:03.000Z',
        status: 'idle',
      },
    ],
    loading: mocks.loading,
    loadingMore: false,
    error: mocks.error,
    isRealtimeSyncing: false,
    searchText: '',
    setSearchText: mocks.setSearchText,
    refresh: vi.fn(),
    loadMore: mocks.loadMore,
    hasMore: true,
    isUnread: (item: { action_id?: string }) => item.action_id === mocks.unreadActionId,
    removeItem: mocks.removeItem,
  }),
}));

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    },
  });
});

afterEach(() => {
  cleanup();
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
  }
  mocks.error = 'history.error.fetchFailed';
  mocks.itemsOverride = null;
  mocks.loading = false;
  mocks.unreadActionId = 'A1';
  vi.clearAllMocks();
});

it('空状態でも起動ボタンは右上の1つだけで、keyboardから開ける', async () => {
  const openNewConversation = vi.fn(async () => undefined);
  window.electron = { history: { openNewConversation } } as unknown as Window['electron'];
  const { rerender } = render(<SuggestionHistoryPage />);

  expect(HISTORY_MESSAGES.ja['history.newConversation']).toBe('新しい会話');
  expect(HISTORY_MESSAGES.en['history.newConversation']).toBe('New conversation');
  expect(screen.getAllByRole('button', { name: 'history.newConversation' })).toHaveLength(1);
  const cta = screen.getByRole('button', { name: 'history.newConversation' });
  expect(cta.closest('.history-toolbar')).not.toBeNull();
  cta.focus();
  await userEvent.keyboard('{Enter}');
  expect(openNewConversation).toHaveBeenCalledOnce();
  expect(openNewConversation).toHaveBeenCalledWith();

  mocks.error = null;
  mocks.itemsOverride = [];
  rerender(<SuggestionHistoryPage />);
  expect(screen.getAllByRole('button', { name: 'history.newConversation' })).toHaveLength(1);
  expect(screen.getByRole('button', { name: 'history.newConversation' })).toBe(cta);
  expect(cta).toHaveFocus();
  // No shortcut bridge: the empty state points at the button alone.
  expect(screen.getByText('history.empty.startWithButton')).toBeInTheDocument();
  expect(HISTORY_MESSAGES.ja['history.empty.startWithShortcut']).toContain('{shortcut}');

  openNewConversation.mockRejectedValueOnce(new Error('unavailable'));
  await userEvent.click(cta);
  expect(await screen.findByRole('alert')).toHaveTextContent('history.openOverlayFailed');
});

it('設定中のショートカットはCTAの中に薄いキーキャップで出し、名前は変えない', async () => {
  const getState = vi.fn(async () => ({ accelerator: 'Option+Space', failure: null }));
  window.electron = {
    process: { platform: 'darwin' },
    history: { openNewConversation: vi.fn(async () => undefined) },
    shortcut: { getState },
  } as unknown as Window['electron'];
  const { container, rerender } = render(<SuggestionHistoryPage />);

  const cta = screen.getByRole('button', { name: 'history.newConversation' });
  // While the shortcut loads, the button shows nothing extra.
  expect(cta.querySelector('.shortcut-keycaps')).toBeNull();
  expect(cta).not.toHaveAttribute('title');
  await waitFor(() => expect(cta).toHaveAttribute('aria-keyshortcuts', 'Alt+Space'));
  const keys = cta.querySelector('.history-new-conversation-keys');
  expect(keys).toHaveAttribute('aria-hidden', 'true');
  expect([...keys!.querySelectorAll('kbd')].map((key) => key.textContent)).toEqual(['⌥', 'Space']);
  expect(cta).toHaveAccessibleName('history.newConversation');
  expect(container.querySelectorAll('.shortcut-keycaps')).toHaveLength(1);
  expect(translate('ja', 'shortcut.hint.label', { keys: 'Option Space' })).toBe(
    'ショートカット: Option Space'
  );
  expect(COMMON_MESSAGES.en['shortcut.hint.label']).toBe('Shortcut: {keys}');

  mocks.error = null;
  mocks.itemsOverride = [];
  rerender(<SuggestionHistoryPage />);
  // The empty state names the shortcut inside the sentence instead of repeating the button.
  const hint = container.querySelector('.history-empty-hint');
  expect(hint?.querySelector('.shortcut-keycaps')).not.toBeNull();
  expect(HISTORY_MESSAGES.ja['history.empty.startWithShortcut'].split('{shortcut}')).toHaveLength(
    2
  );
  expect(HISTORY_MESSAGES.en['history.empty.startWithShortcut'].split('{shortcut}')).toHaveLength(
    2
  );
});

it('ショートカットが登録できていないときはキーキャップを出さず、CTAの説明で伝える', async () => {
  window.electron = {
    process: { platform: 'darwin' },
    shortcut: {
      getState: vi.fn(async () => ({
        accelerator: 'Option+Space',
        failure: 'registration_unavailable',
      })),
    },
  } as unknown as Window['electron'];
  render(<SuggestionHistoryPage />);

  const cta = screen.getByRole('button', { name: 'history.newConversation' });
  await waitFor(() => expect(cta).toHaveAttribute('title', 'shortcut.hint.unavailable'));
  expect(cta).toHaveAccessibleDescription('shortcut.hint.unavailable');
  expect(cta).not.toHaveAttribute('aria-keyshortcuts');
  expect(cta.querySelector('.shortcut-keycaps')).toBeNull();
});

it('行は最終更新の日ごとに、今日・昨日・日付の見出しの下にまとめる', () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 9, 2, 9));
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  const row = (id: string, updatedAt: Date) => ({
    kind: 'suggestion' as const,
    suggestion_id: id,
    title: id,
    updated_at: updatedAt.toISOString(),
    status: 'idle' as const,
  });
  mocks.itemsOverride = [
    row('T1', new Date(2026, 9, 2, 8)),
    row('T2', new Date(2026, 9, 2, 0)),
    row('Y1', new Date(2026, 9, 1, 23)),
    row('O1', new Date(2026, 8, 29, 8)),
  ];
  try {
    render(<SuggestionHistoryPage />);

    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'history.day.today',
      'history.day.yesterday',
      '9月29日',
    ]);
    // Under today and yesterday a row shows its time; older rows keep the full date.
    expect(screen.getByRole('button', { name: /^T1/ })).toHaveTextContent(/^T108:00$/);
    expect(screen.getByRole('button', { name: /^O1/ })).toHaveTextContent('2026年9月29日 08:00');
    expect(HISTORY_MESSAGES.ja['history.day.today']).toBe('今日');
    expect(HISTORY_MESSAGES.en['history.day.yesterday']).toBe('Yesterday');
  } finally {
    vi.useRealTimers();
  }
});

it('Conversation行はOverlayを開き、実際の表示前に既読にしない', async () => {
  const openConversation = vi.fn(async () => 'created' as const);
  const showHistory = vi.fn();
  window.electron = {
    agentOverlay: { showHistory },
    history: { openConversation, markCompletionViewed: mocks.markCompletionViewed },
  } as unknown as Window['electron'];
  render(<SuggestionHistoryPage />);

  const conversation = screen.getByRole('button', { name: /^Conversation/ });
  expect(conversation).not.toHaveAttribute('aria-expanded');
  expect(screen.getByLabelText('history.unread')).toBeInTheDocument();
  expect(screen.getAllByText((text) => text.endsWith(':02'))).toHaveLength(2);
  conversation.focus();
  await userEvent.keyboard('{Enter}');
  expect(openConversation).toHaveBeenCalledWith({ actionId: 'A1' });
  expect(mocks.markCompletionViewed).not.toHaveBeenCalled();
  expect(screen.queryByRole('region', { name: 'history.detail.conversation' })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: /^Suggestion/ }));
  expect(showHistory).toHaveBeenCalledWith({
    suggestionId: 'S1',
    initialUiState: { expand: true },
    fromStart: true,
  });
});

it('履歴の取得失敗は読み上げソフトに届くalertとして出す', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.unreadActionId = null;
  const { rerender } = render(<SuggestionHistoryPage />);

  expect(screen.getByRole('alert')).toHaveTextContent('history.error.fetchFailed');

  // 1件も出せなかったときの取得失敗。
  mocks.itemsOverride = [];
  rerender(<SuggestionHistoryPage />);
  expect(screen.getByRole('alert')).toHaveTextContent('history.error.fetchFailed');
});

it('Overlay起動失敗を通知し、追加読み込みと検索に応答する', async () => {
  const openConversation = vi.fn(async () => 'focused' as const);
  window.electron = { history: { openConversation } } as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  render(<SuggestionHistoryPage />);

  await userEvent.click(screen.getByRole('button', { name: /^Conversation/ }));
  openConversation.mockRejectedValueOnce(new Error('unavailable'));
  await userEvent.click(screen.getByRole('button', { name: /^Conversation/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('history.openOverlayFailed');

  fireEvent.click(screen.getByRole('button', { name: 'history.loadMore' }));
  expect(mocks.loadMore).toHaveBeenCalledOnce();
  const search = screen.getByRole('searchbox', { name: 'history.search' });
  fireEvent.change(search, { target: { value: `${'😀'.repeat(256)}x` } });
  expect(search).toHaveValue('😀'.repeat(256));
  await waitFor(() => expect(mocks.setSearchText).toHaveBeenCalledWith('😀'.repeat(256)));
});

it('バッジは running / approval_pending だけに出し、idle には出さない', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  mocks.itemsOverride = [
    {
      kind: 'conversation',
      action_id: 'A-running',
      title: 'Running',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'running',
      latest_completion_event_id: null,
    },
    {
      kind: 'conversation',
      action_id: 'A-approval',
      title: 'Approval',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      latest_completion_event_id: null,
    },
    {
      kind: 'conversation',
      action_id: 'A-idle',
      title: 'Idle',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      latest_completion_event_id: null,
    },
  ];
  const { container } = render(<SuggestionHistoryPage />);

  expect([...container.querySelectorAll('.badge')].map((badge) => badge.textContent)).toEqual([
    'history.status.running',
    'history.status.approvalPending',
  ]);
});

function livePage(
  actionId: string,
  status: ActionConversationPage['action']['status'],
  entries: ActionConversationPage['runs'][number]['entries']
): ActionConversationPage {
  return {
    action: {
      action_id: actionId,
      suggestion_id: null,
      approved_suggestion: null,
      status,
      latest_run_id: `${actionId}-run`,
      resumable: false,
    },
    runs: [
      {
        run_id: `${actionId}-run`,
        status: status === 'success' ? 'success' : 'running',
        started_at: '2026-10-02T00:00:00.000000Z',
        completed_at: status === 'success' ? '2026-10-02T00:01:00.000000Z' : null,
        completion_event_id: status === 'success' ? 'C' : null,
        entries,
        final_output: status === 'success' ? 'Done' : null,
        error: null,
      },
    ],
    unadopted_messages: [],
    next_cursor: null,
  };
}

function liveUpdate(
  page: ActionConversationPage,
  approvalBlockers: ActionLiveSnapshot['approvalBlockers'] = []
): ActionLiveUpdate {
  const snapshot: ActionLiveSnapshot = {
    actionId: page.action.action_id,
    page,
    pageVersion: 1,
    transientToolSteps: [],
    approvalBlockers,
    lifecycle: null,
  };
  return { kind: 'action_updated', snapshot };
}

it('実行中の会話だけ行の下に今の動きを1行で出し、終われば消す。行は作り直さない', () => {
  const listeners = new Set<(update: ActionLiveUpdate) => void>();
  window.electron = {
    actions: {
      onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
        listeners.add(callback);
        return () => listeners.delete(callback);
      },
    },
  } as unknown as Window['electron'];
  const publish = (update: ActionLiveUpdate) =>
    act(() => listeners.forEach((listener) => listener(update)));
  mocks.error = null;
  mocks.unreadActionId = null;
  const conversation = (actionId: string): ConversationHistoryListItem => ({
    kind: 'conversation',
    action_id: actionId,
    title: actionId,
    updated_at: '2026-08-30T01:02:03.000Z',
    status: 'running',
    latest_completion_event_id: null,
  });
  mocks.itemsOverride = [conversation('A1'), conversation('A2')];
  const { container } = render(<SuggestionHistoryPage />);
  const lines = () =>
    [...container.querySelectorAll('.history-item')].map(
      (row) => row.querySelector('.history-item-live')?.textContent ?? null
    );
  const row = screen.getByRole('button', { name: /^A1/ });
  row.focus();
  expect(lines()).toEqual([null, null]);

  publish(
    liveUpdate(
      livePage('A1', 'processing', [
        {
          step_kind: 'tool',
          step_id: 'tool-2',
          step_number: 2,
          label: 'read',
          status: 'processing',
          outcome: 'completed',
          subject: 'notes.md',
          output_preview: null,
          output_available: false,
          images: [],
        },
        { step_kind: 'assistant', step_id: 'assistant-1', step_number: 1, content: 'Looking' },
      ])
    )
  );
  publish(liveUpdate(livePage('A2', 'queued', [])));
  expect(lines()).toEqual(['notes.md を読み取っています', 'overlay.thinking']);
  // Visual only: the line stays out of the row's accessible name and is not a live region.
  expect(row).toHaveAccessibleName(/^A1/);
  expect(row).not.toHaveAccessibleName(/読み取っています/);
  expect(container.querySelector('.history-item-live')).toHaveAttribute('aria-hidden', 'true');

  const found = livePage('A1', 'processing', [
    { step_kind: 'assistant', step_id: 'assistant-3', step_number: 3, content: 'Found it\nmore' },
  ]);
  publish(liveUpdate(found));
  publish(liveUpdate(livePage('A2', 'success', [])));
  expect(lines()).toEqual(['Found it', null]);
  expect(screen.getByRole('button', { name: /^A1/ })).toBe(row);
  expect(row).toHaveFocus();

  // While an approval is pending the badge says so; the line would only repeat it.
  publish(
    liveUpdate(found, [
      {
        actionId: 'A1',
        processId: 'A1-run',
        approvalSessionId: 'session-1',
        toolRequestId: 'request-1',
        toolId: 'bash',
        intentClass: 'write',
        commandSummary: {},
      },
    ])
  );
  expect(lines()).toEqual([null, null]);
  publish(liveUpdate(found));
  expect(lines()).toEqual(['Found it', null]);

  publish({ kind: 'reset' });
  expect(lines()).toEqual([null, null]);
});

it('日の見出しが増えたり行が別の日へ移ったりしても、残った行はフォーカスを保つ', () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 9, 2, 9));
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  const row = (id: string, updatedAt: Date) => ({
    kind: 'suggestion' as const,
    suggestion_id: id,
    title: id,
    updated_at: updatedAt.toISOString(),
    status: 'idle' as const,
  });
  mocks.itemsOverride = [
    row('Y1', new Date(2026, 9, 1, 8)),
    row('O1', new Date(2026, 8, 29, 8)),
    row('O2', new Date(2026, 8, 29, 7)),
  ];
  try {
    const { rerender } = render(<SuggestionHistoryPage />);
    const older = screen.getByRole('button', { name: /^O1/ });
    older.focus();

    // A live update brings a first row for today, and O2 moves to today as well.
    mocks.itemsOverride = [
      row('N1', new Date(2026, 9, 2, 8)),
      row('O2', new Date(2026, 9, 2, 7)),
      row('Y1', new Date(2026, 9, 1, 8)),
      row('O1', new Date(2026, 8, 29, 8)),
    ];
    rerender(<SuggestionHistoryPage />);

    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'history.day.today',
      'history.day.yesterday',
      '9月29日',
    ]);
    expect(screen.getByRole('button', { name: /^O1/ })).toBe(older);
    expect(older).toHaveFocus();
  } finally {
    vi.useRealTimers();
  }
});

function installDeleteBridge(result: { ok: true } | { ok: false; errorCode: string | null }) {
  const deleteItem = vi.fn(async () => result);
  window.electron = { history: { deleteItem } } as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  return deleteItem;
}

it('削除の確認はキャンセルが初期フォーカスで、キャンセルもEscも削除せず元のボタンへ戻る', async () => {
  const deleteItem = installDeleteBridge({ ok: true });
  render(<SuggestionHistoryPage />);

  const trigger = screen.getByRole('button', { name: 'common.delete Conversation' });
  await userEvent.click(trigger);
  const dialog = screen.getByRole('dialog', { name: 'history.delete.confirmTitle' });
  expect(dialog).toHaveAccessibleDescription('history.delete.confirmBody');
  expect(screen.getByRole('button', { name: 'common.cancel' })).toHaveFocus();
  await userEvent.keyboard('{Enter}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(trigger).toHaveFocus();

  await userEvent.click(trigger);
  fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(trigger).toHaveFocus();
  expect(deleteItem).not.toHaveBeenCalled();
  expect(mocks.removeItem).not.toHaveBeenCalled();
});

it('削除すると行を一覧から外し、次の行へフォーカスを移す', async () => {
  const deleteItem = installDeleteBridge({ ok: true });
  render(<SuggestionHistoryPage />);

  await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(deleteItem).toHaveBeenCalledWith({ kind: 'conversation', id: 'A1' });
  expect(mocks.removeItem).toHaveBeenCalledWith('conversation:A1');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button', { name: /^Suggestion/ })).toHaveFocus();

  // A Suggestion row is deleted the same way, by its own id.
  await userEvent.click(screen.getByRole('button', { name: 'common.delete Suggestion' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(deleteItem).toHaveBeenLastCalledWith({ kind: 'suggestion', id: 'S1' });
});

it('実行中で断られたら短い通知を出し、行を残して削除ボタンへ戻る', async () => {
  installDeleteBridge({ ok: false, errorCode: 'CONVERSATION_BUSY' });
  render(<SuggestionHistoryPage />);

  const trigger = screen.getByRole('button', { name: 'common.delete Conversation' });
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('history.delete.busy');
  expect(mocks.removeItem).not.toHaveBeenCalled();
  expect(trigger).toHaveFocus();
  expect(HISTORY_MESSAGES.ja['history.delete.busy']).toBe(
    'この会話は実行中のため、いまは削除できません。'
  );
});

it('ほかの失敗は削除失敗として通知する', async () => {
  const deleteItem = installDeleteBridge({
    ok: false,
    errorCode: 'CONVERSATION_HISTORY_DELETE_FAILED',
  });
  render(<SuggestionHistoryPage />);

  await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('history.delete.failed');

  deleteItem.mockRejectedValueOnce(new Error('bridge failed'));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('history.delete.failed');
  expect(mocks.removeItem).not.toHaveBeenCalled();
});

it('実行中・確認待ちの会話は削除できず、返事待ちの提案は削除できる', () => {
  installDeleteBridge({ ok: true });
  mocks.itemsOverride = [
    {
      kind: 'conversation',
      action_id: 'A-running',
      title: 'Running',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'running',
      latest_completion_event_id: null,
    },
    {
      kind: 'conversation',
      action_id: 'A-approval',
      title: 'Approval',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      latest_completion_event_id: null,
    },
    {
      kind: 'suggestion',
      suggestion_id: 'S-offer',
      title: 'Offer',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
    },
  ];
  render(<SuggestionHistoryPage />);

  expect(screen.getByRole('button', { name: 'common.delete Running' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'common.delete Approval' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'common.delete Offer' })).toBeEnabled();
});
