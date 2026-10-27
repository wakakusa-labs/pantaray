import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import type {
  ActionLiveSnapshot,
  ActionLiveUpdate,
} from '../../../electron/src/actions/actionLiveCore';
import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';

import { ChatUnreadContext } from '@/components/chat/chatUnread';
import type { HistorySelection } from '@/history/historySelection';
import { useSuggestionHistory } from '@/hooks/useSuggestionHistory';
import { HISTORY_MESSAGES } from '@/i18n/messageCatalog/history';
import type { WorkspaceProjects } from './HistoryProjects';
import { HistorySidebar } from './HistorySidebar';

const mocks = vi.hoisted(() => ({
  chatUnread: 0,
  select: vi.fn(),
  selected: 'chat' as HistorySelection,
  error: 'history.error.fetchFailed' as string | null,
  itemsOverride: null as ConversationHistoryListItem[] | null,
  loading: false,
  loadingMore: false,
  hasMore: true,
  loadMore: vi.fn(),
  markCompletionViewed: vi.fn(async () => undefined),
  removeItem: vi.fn(),
  setSearchText: vi.fn(),
  unreadActionId: 'A1' as string | null,
  startCreatingProject: vi.fn(),
  workspace: null as WorkspaceProjects['settings'],
}));
vi.mock('@/context/useI18n', () => ({
  useI18n: () => ({
    language: 'ja',
    t: (key: string, vars?: Record<string, string | number>) =>
      vars ? `${key} ${JSON.stringify(vars)}` : key,
  }),
}));
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
        status_line: null,
        artifact_names: [],
      },
      {
        kind: 'suggestion',
        suggestion_id: 'S1',
        title: 'Suggestion',
        updated_at: '2026-08-30T01:02:03.000Z',
        status: 'idle',
        status_line: null,
        artifact_names: [],
      },
    ],
    loading: mocks.loading,
    loadingMore: mocks.loadingMore,
    error: mocks.error,
    isRealtimeSyncing: false,
    searchText: '',
    setSearchText: mocks.setSearchText,
    loadMore: mocks.loadMore,
    hasMore: mocks.hasMore,
    isUnread: (item: { action_id?: string }) => item.action_id === mocks.unreadActionId,
    removeItem: mocks.removeItem,
  }),
}));

function Sidebar() {
  return (
    <HistorySidebar
      history={useSuggestionHistory()}
      projects={
        {
          settings: mocks.workspace,
          errorMessage: null,
          pending: new Set(),
          dragController: {},
          busy: false,
        } as unknown as WorkspaceProjects
      }
      creatingProject={false}
      onCreatingProjectChange={mocks.startCreatingProject}
      selected={mocks.selected}
      onSelect={mocks.select}
      hidden={false}
    />
  );
}

function SidebarWrapper({ children }: { children: ReactNode }) {
  return (
    <ChatUnreadContext.Provider value={mocks.chatUnread}>{children}</ChatUnreadContext.Provider>
  );
}

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
  vi.useRealTimers();
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
  }
  mocks.error = 'history.error.fetchFailed';
  mocks.itemsOverride = null;
  mocks.loading = false;
  mocks.loadingMore = false;
  mocks.hasMore = true;
  mocks.unreadActionId = 'A1';
  mocks.chatUnread = 0;
  mocks.selected = 'chat';
  mocks.workspace = null;
  vi.clearAllMocks();
});

const WORKSPACE: NonNullable<WorkspaceProjects['settings']> = {
  read_access_scope: 'workspace',
  organizations: [],
  projects: [
    { project_id: 'p-1', display_name: 'aurora', sort_order: 0, organization_ids: [] },
    { project_id: 'p-2', display_name: 'billing', sort_order: 1, organization_ids: [] },
  ],
  folders: [
    {
      folder_id: 'f-1',
      display_name: 'aurora',
      real_path: '/Users/me/aurora',
      canonical_real_path: '/Users/me/aurora',
      organization_ids: [],
      project_ids: ['p-1'],
    },
  ],
};

it('上から検索、プロジェクト、チャット、作業の順に並べ、＋で追加の流れを呼ぶ', async () => {
  mocks.workspace = WORKSPACE;
  window.electron = {} as unknown as Window['electron'];
  render(<Sidebar />, { wrapper: SidebarWrapper });

  const order = [
    screen.getByRole('search'),
    screen.getByRole('region', { name: 'history.projects.title' }),
    screen.getByRole('button', { name: 'history.chat.title' }),
    screen.getByRole('region', { name: 'history.tasks.title' }),
    screen.getByRole('button', { name: /^Conversation/ }),
  ];
  for (const [earlier, later] of order.slice(0, -1).map((node, i) => [node, order[i + 1]]))
    expect(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const section = screen.getByRole('region', { name: 'history.projects.title' });
  const projectRows = within(section).getAllByRole('button', { name: /^(aurora|billing)$/u });
  expect(projectRows.map((row) => row.textContent)).toEqual(['aurora', 'billing']);

  const add = within(section).getByRole('button', { name: 'history.projects.add' });
  add.focus();
  await userEvent.keyboard('{Enter}');
  expect(mocks.startCreatingProject).toHaveBeenCalledWith(true);
});

it('作業の見出しは作業が無くても出し、✎ はメイン画面に、隣の小さく開くはショートカットと同じ小さい作業を開く', async () => {
  mocks.error = null;
  mocks.itemsOverride = [];
  const openNewConversation = vi.fn(async () => undefined);
  window.electron = {
    process: { platform: 'darwin' },
    history: { openNewConversation },
    shortcut: { getState: vi.fn(async () => ({ accelerator: 'Option+Space', failure: null })) },
  } as unknown as Window['electron'];
  render(<Sidebar />, { wrapper: SidebarWrapper });

  const tasks = screen.getByRole('region', { name: 'history.tasks.title' });
  expect(HISTORY_MESSAGES.ja['history.tasks.title']).toBe('作業');
  expect(HISTORY_MESSAGES.en['history.tasks.title']).toBe('Tasks');
  expect(HISTORY_MESSAGES.ja['history.newTaskMain']).toBe('メイン画面で新しい作業');
  expect(HISTORY_MESSAGES.ja['history.newTaskSmall']).toBe('小さく新しい作業');
  // Pen first, then the small window, as one row beside the heading.
  expect(
    within(tasks)
      .getAllByRole('button')
      .map((button) => button.getAttribute('aria-label'))
  ).toEqual(['history.newTaskMain', 'history.newTaskSmall']);
  const main = within(tasks).getByRole('button', { name: 'history.newTaskMain' });
  main.focus();
  await userEvent.keyboard('{Enter}');
  expect(mocks.select).toHaveBeenCalledWith('new');
  expect(openNewConversation).not.toHaveBeenCalled();

  const small = within(tasks).getByRole('button', { name: 'history.newTaskSmall' });
  await waitFor(() => expect(small).toHaveAttribute('aria-keyshortcuts', 'Alt+Space'));
  expect(small).toHaveAttribute(
    'data-tooltip',
    'history.newTaskSmallWithShortcut {"shortcut":"⌥ Space"}'
  );
  expect(HISTORY_MESSAGES.ja['history.newTaskSmallWithShortcut']).toBe(
    '小さく新しい作業（{shortcut}）'
  );
  small.focus();
  await userEvent.keyboard('{Enter}');
  expect(openNewConversation).toHaveBeenCalledOnce();
  expect(mocks.select).toHaveBeenCalledOnce();

  openNewConversation.mockRejectedValueOnce(new Error('unavailable'));
  await userEvent.click(small);
  expect(await screen.findByRole('alert')).toHaveTextContent('history.newTaskSmallFailed');
});

it('ショートカットが登録できていなければ、小さく開くの案内は名前だけにする', async () => {
  const getState = vi.fn(async () => ({
    accelerator: 'Option+Space',
    failure: 'registration_unavailable',
  }));
  window.electron = {
    process: { platform: 'darwin' },
    shortcut: { getState },
  } as unknown as Window['electron'];
  render(<Sidebar />, { wrapper: SidebarWrapper });

  await waitFor(() => expect(getState).toHaveBeenCalledOnce());
  // Let the read settle, so the assertions see its outcome rather than the loading state.
  await act(async () => {});
  const small = screen.getByRole('button', { name: 'history.newTaskSmall' });
  expect(small).toHaveAttribute('data-tooltip', 'history.newTaskSmall');
  expect(small).not.toHaveAttribute('aria-keyshortcuts');
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
    status_line: null,
    artifact_names: [],
  });
  mocks.itemsOverride = [
    row('T1', new Date(2026, 9, 2, 8)),
    row('T2', new Date(2026, 9, 2, 0)),
    row('Y1', new Date(2026, 9, 1, 23)),
    row('O1', new Date(2026, 8, 29, 8)),
  ];
  try {
    render(<Sidebar />, { wrapper: SidebarWrapper });

    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      'history.day.today',
      'history.day.yesterday',
      '9月29日',
    ]);
    // A row is its title; the day heading carries the date.
    expect(screen.getByRole('button', { name: /^T1/ })).toHaveTextContent(/^T1$/);
    expect(HISTORY_MESSAGES.ja['history.day.today']).toBe('今日');
    expect(HISTORY_MESSAGES.en['history.day.yesterday']).toBe('Yesterday');
  } finally {
    vi.useRealTimers();
  }
});

it('行は選ぶだけでOverlayを開かず、実際の表示前に既読にしない。選んだ行を表示中として示す', async () => {
  const openConversation = vi.fn(async () => 'created' as const);
  const showHistory = vi.fn();
  window.electron = {
    agentOverlay: { showHistory },
    history: { openConversation, markCompletionViewed: mocks.markCompletionViewed },
  } as unknown as Window['electron'];
  const { rerender } = render(<Sidebar />, { wrapper: SidebarWrapper });

  const conversation = screen.getByRole('button', { name: /^Conversation/ });
  const chat = screen.getByRole('button', { name: 'history.chat.title' });
  expect(conversation).not.toHaveAttribute('aria-expanded');
  expect(conversation).not.toHaveAttribute('aria-current');
  // Only the conversation with an unseen completion has the dot, inside its own row.
  expect(screen.getAllByRole('img', { name: 'history.unread' })).toHaveLength(1);
  expect(conversation).toHaveAccessibleName(/history\.unread/);
  conversation.focus();
  await userEvent.keyboard('{Enter}');
  expect(mocks.select).toHaveBeenLastCalledWith('action:A1');

  fireEvent.click(screen.getByRole('button', { name: /^Suggestion/ }));
  expect(mocks.select).toHaveBeenLastCalledWith('suggestion:S1');
  fireEvent.click(chat);
  expect(mocks.select).toHaveBeenLastCalledWith('chat');
  expect(openConversation).not.toHaveBeenCalled();
  expect(showHistory).not.toHaveBeenCalled();
  expect(mocks.markCompletionViewed).not.toHaveBeenCalled();

  mocks.selected = 'action:A1';
  rerender(<Sidebar />);
  expect(conversation).toHaveAttribute('aria-current', 'true');
  expect(chat).not.toHaveAttribute('aria-current');
  expect(screen.getByRole('button', { name: /^Suggestion/ })).not.toHaveAttribute('aria-current');
  expect(conversation.closest('.history-item')).toBeInstanceOf(HTMLLIElement);
});

it('履歴の取得失敗は読み上げソフトに届くalertとして出す', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.unreadActionId = null;
  const { rerender } = render(<Sidebar />, {
    wrapper: SidebarWrapper,
  });

  expect(screen.getByRole('alert')).toHaveTextContent('history.error.fetchFailed');

  // 1件も出せなかったときの取得失敗。
  mocks.itemsOverride = [];
  rerender(<Sidebar />);
  expect(screen.getByRole('alert')).toHaveTextContent('history.error.fetchFailed');
});

it('作業の終わりが見えると次を一度だけ読み、読み込み中は読まず、最後まで来たら止める', () => {
  const observers: {
    callback: IntersectionObserverCallback;
    options?: IntersectionObserverInit;
    target?: Element;
    disconnected: boolean;
  }[] = [];
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      private record;
      constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
        this.record = { callback, options, disconnected: false } as (typeof observers)[number];
        observers.push(this.record);
      }
      observe(target: Element) {
        this.record.target = target;
      }
      disconnect() {
        this.record.disconnected = true;
      }
    }
  );
  const reach = (isIntersecting: boolean) => {
    const live = observers.filter((observer) => !observer.disconnected);
    act(() =>
      live.forEach(({ callback }) =>
        callback([{ isIntersecting } as IntersectionObserverEntry], {} as IntersectionObserver)
      )
    );
  };
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  try {
    const { container, rerender } = render(<Sidebar />, { wrapper: SidebarWrapper });
    const end = container.querySelector('.history-list-end');
    const live = observers.filter((observer) => !observer.disconnected);
    expect(live).toHaveLength(1);
    expect(live[0].target).toBe(end);
    expect(live[0].options?.root).toBe(container.querySelector('.history-sidebar__tasks'));
    expect(live[0].options?.rootMargin).toBeTruthy();
    expect(screen.queryByRole('button', { name: /history\.load/u })).toBeNull();

    reach(false);
    expect(mocks.loadMore).not.toHaveBeenCalled();
    reach(true);
    expect(mocks.loadMore).toHaveBeenCalledOnce();

    // While the page is on its way, the list says so to assistive technology and loads no more.
    mocks.loadingMore = true;
    rerender(<Sidebar />);
    expect(container.querySelector('.history-list')).toHaveAttribute('aria-busy', 'true');
    reach(true);
    expect(mocks.loadMore).toHaveBeenCalledOnce();

    // The last page arrived: nothing is left to watch.
    mocks.loadingMore = false;
    mocks.hasMore = false;
    rerender(<Sidebar />);
    expect(container.querySelector('.history-list-end')).toBeNull();
    expect(observers.every((observer) => observer.disconnected)).toBe(true);
    reach(true);
    expect(mocks.loadMore).toHaveBeenCalledOnce();
  } finally {
    vi.unstubAllGlobals();
  }
});

it('検索に応答する', async () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  render(<Sidebar />, { wrapper: SidebarWrapper });

  const search = screen.getByRole('searchbox', { name: 'history.search' });
  fireEvent.change(search, { target: { value: `${'😀'.repeat(256)}x` } });
  expect(search).toHaveValue('😀'.repeat(256));
  await waitFor(() => expect(mocks.setSearchText).toHaveBeenCalledWith('😀'.repeat(256)));
});

it('確認待ちの会話と返事待ちの提案はチップ、未読の完了だけ点で示し、どれも理由で読み上げる', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = 'A-unread';
  mocks.itemsOverride = [
    {
      kind: 'conversation',
      action_id: 'A-running',
      title: 'Running',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'running',
      latest_completion_event_id: null,
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'conversation',
      action_id: 'A-approval',
      title: 'Approval',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      latest_completion_event_id: null,
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'conversation',
      action_id: 'A-idle',
      title: 'Idle',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      latest_completion_event_id: null,
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'conversation',
      action_id: 'A-unread',
      title: 'Unread',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      latest_completion_event_id: 'C1',
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'suggestion',
      suggestion_id: 'S-offer',
      title: 'Offer',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'suggestion',
      suggestion_id: 'S-decided',
      title: 'Decided',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      status_line: null,
      artifact_names: [],
    },
  ];
  const { container } = render(<Sidebar />, {
    wrapper: SidebarWrapper,
  });

  expect(
    [...container.querySelectorAll('.badge')].map((chip) => [chip.className, chip.textContent])
  ).toEqual([
    ['badge badge--warning', 'history.row.approvalNeeded'],
    ['badge badge--info', 'history.status.suggestion'],
  ]);
  expect(
    [...container.querySelectorAll('.history-item-unread')].map((dot) =>
      dot.getAttribute('aria-label')
    )
  ).toEqual(['history.unread']);
  const dotOf = (title: string) =>
    screen
      .getByRole('button', { name: new RegExp(`^${title}`) })
      .querySelector('[role="img"]')
      ?.getAttribute('aria-label') ?? null;
  expect(
    ['Running', 'Approval', 'Idle', 'Unread', 'Offer', 'Decided'].map((title) => [
      title,
      dotOf(title),
    ])
  ).toEqual([
    ['Running', null],
    ['Approval', 'history.status.approvalPending'],
    ['Idle', null],
    ['Unread', 'history.unread'],
    ['Offer', 'history.attention.suggestion'],
    ['Decided', null],
  ]);
  // A chip shows what waits in a word and is heard with its reason, as the dot was.
  expect(screen.getByRole('button', { name: /^Approval/ })).toHaveTextContent(
    /^Approvalhistory\.row\.approvalNeeded$/
  );
  expect(screen.getByRole('button', { name: /^Approval/ })).toHaveAccessibleName(
    'Approval history.status.approvalPending'
  );
  expect(screen.getByRole('button', { name: /^Running/ })).toHaveAccessibleName(
    'Running history.status.running'
  );
  expect(screen.getByRole('button', { name: /^Idle/ })).toHaveAccessibleName('Idle');
  expect(screen.getByRole('button', { name: /^Offer/ })).toHaveAccessibleName(
    'Offer history.attention.suggestion'
  );
  expect(HISTORY_MESSAGES.ja['history.attention.suggestion']).toBe('返事待ちの提案');
  expect(HISTORY_MESSAGES.ja['history.row.approvalNeeded']).toBe('承認待ち');
  expect(HISTORY_MESSAGES.ja['history.status.suggestion']).toBe('提案');
  expect(HISTORY_MESSAGES.en['history.row.approvalNeeded']).toBe('Approval needed');
});

it('行はタイトルの下に今の状況と作ったファイルを1行ずつ出し、名前を変えず説明として伝える', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  mocks.unreadActionId = null;
  mocks.itemsOverride = [
    {
      kind: 'conversation',
      action_id: 'A-done',
      title: 'Done',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      latest_completion_event_id: 'C1',
      status_line: 'Summary ready',
      artifact_names: ['report.md', 'deck.pdf', 'notes.txt'],
    },
    {
      kind: 'suggestion',
      suggestion_id: 'S-offer',
      title: 'Offer',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      status_line: 'The deadline is Friday.',
      artifact_names: ['outline.md'],
    },
    {
      kind: 'conversation',
      action_id: 'A-bare',
      title: 'Bare',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      latest_completion_event_id: null,
      status_line: null,
      artifact_names: [],
    },
  ];
  const { container } = render(<Sidebar />, {
    wrapper: SidebarWrapper,
  });

  const rowOf = (title: string) => screen.getByRole('button', { name: new RegExp(`^${title}`) });
  const linesOf = (title: string) => {
    const row = rowOf(title);
    return [
      row.querySelector('.history-item-line')?.textContent ?? null,
      row.querySelector('.history-item-files')?.textContent ?? null,
    ];
  };
  expect(linesOf('Done')).toEqual(['Summary ready', 'report.md history.row.moreFiles {"count":2}']);
  expect(linesOf('Offer')).toEqual(['The deadline is Friday.', 'outline.md']);
  expect(linesOf('Bare')).toEqual([null, null]);
  expect(rowOf('Done').querySelector('.history-item-files svg')).not.toBeNull();
  // Seen under the title, heard after it: the row keeps its name.
  expect(rowOf('Done')).toHaveAccessibleName('Done');
  expect(rowOf('Done')).toHaveAccessibleDescription(
    'Summary ready report.md history.row.moreFiles {"count":2}'
  );
  expect(rowOf('Offer')).toHaveAccessibleName('Offer history.attention.suggestion');
  expect(rowOf('Offer')).toHaveAccessibleDescription('The deadline is Friday. outline.md');
  expect(rowOf('Bare')).not.toHaveAttribute('aria-describedby');
  expect(container.querySelector('.history-item-line')).toHaveAttribute('aria-hidden', 'true');
  expect(HISTORY_MESSAGES.ja['history.row.moreFiles']).toBe('ほか {count} 件');
  expect(HISTORY_MESSAGES.en['history.row.moreFiles']).toBe('and {count} more');
});

it('チャットの行は表示中として示し、未読の件数を数字と説明で伝える', () => {
  window.electron = {} as unknown as Window['electron'];
  mocks.error = null;
  const { rerender } = render(<Sidebar />, {
    wrapper: SidebarWrapper,
  });

  const chat = screen.getByRole('button', { name: 'history.chat.title' });
  expect(chat).toHaveAttribute('aria-current', 'true');
  expect(chat).not.toHaveAccessibleDescription();
  expect(chat).toHaveTextContent(/^history\.chat\.title$/);

  mocks.chatUnread = 4;
  rerender(<Sidebar />);
  expect(chat).toHaveTextContent(/^history\.chat\.title4$/);
  expect(chat).toHaveAccessibleName('history.chat.title');
  expect(chat).toHaveAccessibleDescription('history.chat.unread {"count":4}');

  mocks.chatUnread = 120;
  rerender(<Sidebar />);
  expect(chat).toHaveTextContent(/99\+$/);
  expect(chat).toHaveAccessibleDescription('history.chat.unread {"count":120}');
  expect(HISTORY_MESSAGES.ja['history.chat.title']).toBe('チャット');
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
  vi.useFakeTimers();
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
    status_line: null,
    artifact_names: [],
  });
  mocks.itemsOverride = [conversation('A1'), conversation('A2')];
  const { container } = render(<Sidebar />, {
    wrapper: SidebarWrapper,
  });
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
          file_edit: null,
        },
        { step_kind: 'assistant', step_id: 'assistant-1', step_number: 1, content: 'Looking' },
      ])
    )
  );
  publish(liveUpdate(livePage('A2', 'queued', [])));
  // A step shows at once; thinking waits a moment, so a quick hop between steps does not blink.
  expect(lines()).toEqual(['notes.md を読み取っています', null]);
  act(() => vi.advanceTimersByTime(600));
  expect(lines()).toEqual(['notes.md を読み取っています', 'overlay.thinking']);
  // Visual only: the line stays out of the row's accessible name and is not a live region.
  expect(row).toHaveAccessibleName(/^A1/);
  expect(row).not.toHaveAccessibleName(/読み取っています/);
  expect(container.querySelector('.history-item-live')).toHaveAttribute('aria-hidden', 'true');

  const found = livePage('A1', 'processing', [
    { step_kind: 'assistant', step_id: 'assistant-3', step_number: 3, content: 'Found it\nmore' },
  ]);
  publish(liveUpdate(found));
  // A finished run's line goes at once; the shown step holds before the next one replaces it.
  publish(liveUpdate(livePage('A2', 'success', [])));
  expect(lines()).toEqual(['notes.md を読み取っています', null]);
  act(() => vi.advanceTimersByTime(600));
  expect(lines()).toEqual(['Found it', null]);
  expect(screen.getByRole('button', { name: /^A1/ })).toBe(row);
  expect(row).toHaveFocus();

  // While an approval is pending the chip says so; the line would only repeat it.
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
    status_line: null,
    artifact_names: [],
  });
  mocks.itemsOverride = [
    row('Y1', new Date(2026, 9, 1, 8)),
    row('O1', new Date(2026, 8, 29, 8)),
    row('O2', new Date(2026, 8, 29, 7)),
  ];
  try {
    const { rerender } = render(<Sidebar />, {
      wrapper: SidebarWrapper,
    });
    const older = screen.getByRole('button', { name: /^O1/ });
    older.focus();

    // A live update brings a first row for today, and O2 moves to today as well.
    mocks.itemsOverride = [
      row('N1', new Date(2026, 9, 2, 8)),
      row('O2', new Date(2026, 9, 2, 7)),
      row('Y1', new Date(2026, 9, 1, 8)),
      row('O1', new Date(2026, 8, 29, 8)),
    ];
    rerender(<Sidebar />);

    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
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
  render(<Sidebar />, { wrapper: SidebarWrapper });

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
  render(<Sidebar />, { wrapper: SidebarWrapper });

  await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(deleteItem).toHaveBeenCalledWith({ kind: 'conversation', id: 'A1' });
  expect(mocks.removeItem).toHaveBeenCalledWith('conversation:A1');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('button', { name: /^Suggestion/ })).toHaveFocus();
  // The row was not the one shown, so the selection stays.
  expect(mocks.select).not.toHaveBeenCalled();

  // A Suggestion row is deleted the same way, by its own id.
  await userEvent.click(screen.getByRole('button', { name: 'common.delete Suggestion' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(deleteItem).toHaveBeenLastCalledWith({ kind: 'suggestion', id: 'S1' });
});

it('表示中の行を削除すると、選択を次の行へ、無ければチャットへ置き換えで移す', async () => {
  installDeleteBridge({ ok: true });
  mocks.selected = 'action:A1';
  const { rerender } = render(<Sidebar />, { wrapper: SidebarWrapper });

  await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(mocks.select).toHaveBeenCalledExactlyOnceWith('suggestion:S1', { replace: true });

  mocks.selected = 'suggestion:S1';
  mocks.itemsOverride = [
    {
      kind: 'suggestion',
      suggestion_id: 'S1',
      title: 'Suggestion',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'idle',
      status_line: null,
      artifact_names: [],
    },
  ];
  rerender(<Sidebar />);
  await userEvent.click(screen.getByRole('button', { name: 'common.delete Suggestion' }));
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(mocks.select).toHaveBeenLastCalledWith('chat', { replace: true });
});

it('実行中で断られたら短い通知を出し、行を残して削除ボタンへ戻る', async () => {
  installDeleteBridge({ ok: false, errorCode: 'CONVERSATION_BUSY' });
  mocks.selected = 'action:A1';
  render(<Sidebar />, { wrapper: SidebarWrapper });

  const trigger = screen.getByRole('button', { name: 'common.delete Conversation' });
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('history.delete.busy');
  expect(mocks.removeItem).not.toHaveBeenCalled();
  // The row stays, and so does the selection.
  expect(mocks.select).not.toHaveBeenCalled();
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
  render(<Sidebar />, { wrapper: SidebarWrapper });

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
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'conversation',
      action_id: 'A-approval',
      title: 'Approval',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      latest_completion_event_id: null,
      status_line: null,
      artifact_names: [],
    },
    {
      kind: 'suggestion',
      suggestion_id: 'S-offer',
      title: 'Offer',
      updated_at: '2026-08-30T01:02:03.000Z',
      status: 'approval_pending',
      status_line: null,
      artifact_names: [],
    },
  ];
  render(<Sidebar />, { wrapper: SidebarWrapper });

  expect(screen.getByRole('button', { name: 'common.delete Running' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'common.delete Approval' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'common.delete Offer' })).toBeEnabled();
});

it('削除の応答を待つ間に選択が変わったら、応答の時点の選択で判断する', async () => {
  let answer: (result: { ok: true }) => void = () => undefined;
  const deleteItem = installDeleteBridge({ ok: true });
  deleteItem.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
  const deleteConversation = async () => {
    await userEvent.click(screen.getByRole('button', { name: 'common.delete Conversation' }));
    await userEvent.click(screen.getByRole('button', { name: 'common.delete' }));
  };

  // Shown when the delete started, then the user moved on: the selection stays theirs.
  mocks.selected = 'action:A1';
  const { rerender } = render(<Sidebar />, { wrapper: SidebarWrapper });
  await deleteConversation();
  mocks.selected = 'suggestion:S1';
  rerender(<Sidebar />);
  await act(async () => answer({ ok: true }));
  expect(mocks.removeItem).toHaveBeenCalledWith('conversation:A1');
  expect(mocks.select).not.toHaveBeenCalled();

  // Not shown when the delete started, then selected: the pane moves off it.
  mocks.selected = 'chat';
  rerender(<Sidebar />);
  await deleteConversation();
  mocks.selected = 'action:A1';
  rerender(<Sidebar />);
  await act(async () => answer({ ok: true }));
  expect(mocks.select).toHaveBeenCalledExactlyOnceWith('suggestion:S1', { replace: true });
});
