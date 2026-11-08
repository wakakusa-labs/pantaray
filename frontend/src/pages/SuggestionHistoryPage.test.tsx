import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ActionConversationUserItem } from '../../electron/src/actions/actionConversationModel';
import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import type {
  DeletedTodoView,
  TodoView,
  TodoWeekPage,
} from '../../electron/src/todos/todoContracts';
import { UserItem } from '@/components/action-conversation/UserMessage';
import type { ChatViewHandle } from '@/components/chat/ChatView';
import type { TaskPreview } from '@/components/task/taskFiles';
import { ChatSessionContext, type ChatSession } from '@/components/chat/chatSession';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { historySelectionSearch, parseHistorySelection } from '@/history/historySelection';
import type { LayoutOutletContext } from '@/components/Layout';
import SuggestionHistoryPage from './SuggestionHistoryPage';

const mocks = vi.hoisted(() => ({
  showNewest: vi.fn(),
  publishOrganizations: vi.fn(),
  todoUx: true,
}));

// On unless a test turns it off: off, the page is History as it was before the TODO list.
vi.mock('@/todos/todoUxFeature', () => ({
  get TODO_UX_ENABLED() {
    return mocks.todoUx;
  },
}));
/** Turns the TODO list off: the sidebar holds the search, the chat and the conversations. */
const withoutTodoList = () => {
  mocks.todoUx = false;
};
// The panes and the chat have their own tests; here they only show what the page gave them.
vi.mock('@/components/AiConnectionNotice', () => ({ AiConnectionNotice: () => null }));
vi.mock('@/components/chat/ChatView', () => ({
  ChatView: forwardRef<ChatViewHandle>(function ChatView(_props, ref) {
    useImperativeHandle(ref, () => ({ showNewest: mocks.showNewest }));
    return <section aria-label="chat" />;
  }),
}));
vi.mock('@/components/task/TaskWorkspace', () => ({
  TaskWorkspace: ({
    actionId,
    title,
    initialPreview,
    onAddProject,
  }: {
    actionId: string;
    title: string;
    initialPreview: TaskPreview | null;
    onAddProject: () => void;
  }) => (
    <section aria-label={`action ${actionId}`}>
      <h1>{title}</h1>
      {initialPreview ? <p>preview {initialPreview.file.path}</p> : null}
      <UserItem item={SENT_NAMING_SUGGESTION} />
      <button type="button" onClick={onAddProject}>
        add project
      </button>
    </section>
  ),
}));
vi.mock('@/components/task/SuggestionTaskPane', () => ({
  SuggestionTaskPane: ({
    suggestionId,
    title,
    canDismiss,
    onStarted,
  }: {
    suggestionId: string;
    title: string;
    canDismiss: boolean;
    onStarted: (actionId: string, preview: TaskPreview | null) => void;
  }) => (
    <section aria-label={`suggestion ${suggestionId}`}>
      <h1>{title}</h1>
      {canDismiss ? <button type="button">dismiss</button> : null}
      <button type="button" onClick={() => onStarted('A9', null)}>
        accept
      </button>
      <button type="button" onClick={() => onStarted('A9', OPEN_REPLY)}>
        reply with a file open
      </button>
    </section>
  ),
}));

vi.mock('@/components/task/NewTaskPane', () => ({
  NewTaskPane: ({ onStarted }: { onStarted: (actionId: string) => void }) => (
    <section aria-label="new task">
      <button type="button" onClick={() => onStarted('A7')}>
        send
      </button>
    </section>
  ),
}));

// A sent message in the task's conversation that names the suggestion S1.
const SENT_NAMING_SUGGESTION: ActionConversationUserItem = {
  kind: 'user',
  source: 'canonical',
  key: 'sent',
  visibility: 'always',
  entry: {
    step_kind: 'user',
    chat_note: null,
    approved_suggestion: null,
    step_id: 'step-sent',
    step_number: 1,
    message_id: 'message-sent',
    accepted_sequence: 1,
    content: '請求書 を見て',
    images: [],
    project_refs: [],
    work_refs: [
      { kind: 'suggestion', id: 'S1', title: '請求書を作りましょうか', start: 0, end: 3 },
    ],
    status: 'adopted',
  },
};

const OPEN_REPLY: TaskPreview = {
  target: { kind: 'suggestion', id: 'S1' },
  file: { path: 'drafts/reply.md', name: 'reply.md', kind: 'markdown', label: 'MD' },
};

const ITEMS: ConversationHistoryListItem[] = [
  {
    kind: 'conversation',
    action_id: 'A1',
    title: '見積書を直す',
    updated_at: '2026-10-10T01:00:00.000Z',
    status: 'idle',
    latest_completion_event_id: null,
    status_line: null,
    artifact_names: [],
  },
  {
    kind: 'suggestion',
    suggestion_id: 'S1',
    title: '請求書を作りましょうか',
    updated_at: '2026-10-10T00:00:00.000Z',
    status: 'approval_pending',
    artifact_names: [],
  },
];
function todo(todoId: string, projectId: string | null, extra: Partial<TodoView> = {}): TodoView {
  return {
    todo_id: todoId,
    project_id: projectId,
    text: `TODO ${todoId}`,
    author: 'user',
    origin: 'written',
    week_start: '2026-11-02',
    carried: false,
    due: null,
    state: { kind: 'working', note: null },
    deletion_request: null,
    conversation: { kind: 'none' },
    ...extra,
  };
}

const WEEK: TodoWeekPage = {
  today: '2026-11-04',
  current_week_start: '2026-11-02',
  week: { start: '2026-11-02', end: '2026-11-08' },
  items: [
    todo('T1', 'P1', { conversation: { kind: 'action', action_id: 'A5' } }),
    todo('T2', 'P1', { state: { kind: 'done', by: 'user', at: '2026-11-03T01:00:00.000Z' } }),
    todo('T3', 'P2', { conversation: { kind: 'suggestion', suggestion_id: 'S5' } }),
    todo('T4', null),
  ],
};
const listWeek = vi.fn(async (_query: { week_start?: string }) => WEEK);
const listDeleted = vi.fn(async (): Promise<DeletedTodoView[]> => []);
/** Every reader that listens for `todos:changed`; the test tells them all, as main does. */
const changeListeners = new Set<() => void>();
const todosChanged = () => changeListeners.forEach((listener) => listener());
const removeTodo = vi.fn(async () => ({ kind: 'refused', code: 'TODO_BUSY' }) as const);
const createTodo = vi.fn(async () => ({ kind: 'ok', result: {} }) as const);
const deleteItem = vi.fn(async () => ({ ok: true }) as const);
const selectFolder = vi.fn(async () => ({ canceled: true, path: null }));

const stored = new Map<string, string>();

beforeEach(() => {
  mocks.todoUx = true;
  stored.clear();
  listWeek.mockImplementation(async () => WEEK);
  listDeleted.mockImplementation(async () => []);
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
  });
  window.electron = {
    process: { platform: 'darwin' },
    history: {
      fetch: vi.fn(async () => ({
        data: ITEMS,
        nextCursor: null,
        unreadActionIds: [],
        error: null,
        errorCode: null,
      })),
      deleteItem,
      openConversation: vi.fn(),
    },
    agentOverlay: { showHistory: vi.fn() },
    todos: {
      listWeek,
      listDeleted,
      onChanged: (callback: () => void) => {
        changeListeners.add(callback);
        return () => changeListeners.delete(callback);
      },
      delete: removeTodo,
      create: createTodo,
    },
    workspaceSettings: {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace',
        organizations: [{ organization_id: 'O1', display_name: 'Xer' }],
        projects: [
          { project_id: 'P1', display_name: '見積もり', sort_order: 0, organization_ids: ['O1'] },
          { project_id: 'P2', display_name: '採用', sort_order: 1, organization_ids: [] },
        ],
        folders: [],
      })),
      selectFolder,
    },
  } as unknown as Window['electron'];
  const originalShowModal = HTMLDialogElement.prototype.showModal;
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  return () => {
    HTMLDialogElement.prototype.showModal = originalShowModal;
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output aria-label="location">{`${location.pathname}${location.search}`}</output>
      <button type="button" onClick={() => navigate(-1)}>
        back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        forward
      </button>
    </>
  );
}

const OUTLET_CONTEXT: LayoutOutletContext = {
  publishOrganizations: mocks.publishOrganizations,
};

const renderPage = (entries: string[]) =>
  render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <UiLanguageProvider initialLanguage="ja">
        <LocalOwnerContext.Provider value={{ id: 'user-1', kind: 'account' }}>
          <ChatSessionContext.Provider value={{} as ChatSession}>
            <Routes>
              <Route element={<Outlet context={OUTLET_CONTEXT} />}>
                <Route path="/history" element={<SuggestionHistoryPage />} />
              </Route>
            </Routes>
            <LocationProbe />
          </ChatSessionContext.Provider>
        </LocalOwnerContext.Provider>
      </UiLanguageProvider>
    </MemoryRouter>
  );

const location = () => screen.getByRole('status', { name: 'location' });

it('shows what `?item=` names, the TODOs without one, and the chat for a malformed one', async () => {
  renderPage(['/history?item=action:A1']);
  expect(screen.getByRole('region', { name: 'action A1' })).toBeInTheDocument();
  // The title is the history row's once the list is read.
  expect(await screen.findByRole('heading', { name: '見積書を直す' })).toBeInTheDocument();
  cleanup();

  renderPage(['/history?item=suggestion:S1']);
  expect(screen.getByRole('region', { name: 'suggestion S1' })).toBeInTheDocument();
  expect(
    await screen.findByRole('heading', { name: '請求書を作りましょうか' })
  ).toBeInTheDocument();
  cleanup();

  // A task the list does not hold is still shown, under a plain title.
  renderPage(['/history?item=action:OLD']);
  expect(
    within(screen.getByRole('region', { name: 'action OLD' })).getByRole('heading', {
      name: '作業',
    })
  ).toBeInTheDocument();
  cleanup();

  renderPage(['/history']);
  expect(screen.getByRole('heading', { level: 1, name: '今週' })).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'chat' })).toBeNull();
  cleanup();

  for (const entry of [
    '/history?item=chat',
    '/history?item=action:',
    '/history?item=action:%20A1',
    '/history?item=conversation:A1',
  ]) {
    renderPage([entry]);
    expect(screen.getByRole('region', { name: 'chat' })).toBeInTheDocument();
    cleanup();
  }
});

it('without the TODO list, shows the chat where `?item=` names nothing, as History did', async () => {
  withoutTodoList();
  renderPage(['/history']);
  expect(screen.getByRole('region', { name: 'chat' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { level: 1, name: '今週' })).toBeNull();
  const sidebar = screen.getByRole('complementary', HISTORY_SIDEBAR);
  expect(within(sidebar).getByRole('button', { name: 'チャット' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  expect(within(sidebar).queryByRole('button', { name: 'すべての TODO' })).toBeNull();
});

it('selects a row into the URL and marks it current; the Chat row of the chat shows the newest', async () => {
  withoutTodoList();
  renderPage(['/history?item=chat']);
  const row = await screen.findByRole('button', { name: /^見積書を直す/ });
  const chatRow = screen.getByRole('button', { name: 'チャット' });
  expect(chatRow).toHaveAttribute('aria-current', 'true');

  await userEvent.click(chatRow);
  expect(mocks.showNewest).toHaveBeenCalledOnce();
  expect(location()).toHaveTextContent(/^\/history\?item=chat$/);

  await userEvent.click(row);
  expect(location()).toHaveTextContent('/history?item=action:A1');
  expect(screen.getByRole('region', { name: 'action A1' })).toBeInTheDocument();
  expect(row).toHaveAttribute('aria-current', 'true');
  expect(chatRow).not.toHaveAttribute('aria-current');

  await userEvent.click(screen.getByRole('button', { name: /^請求書を作りましょうか/ }));
  expect(location()).toHaveTextContent('/history?item=suggestion:S1');

  await userEvent.click(chatRow);
  expect(location()).toHaveTextContent('/history?item=chat');
  expect(screen.getByRole('region', { name: 'chat' })).toBeInTheDocument();
  expect(mocks.showNewest).toHaveBeenCalledOnce();
  // Rows choose the pane; no Overlay opens.
  expect(window.electron?.history?.openConversation).not.toHaveBeenCalled();
  expect(window.electron?.agentOverlay?.showHistory).not.toHaveBeenCalled();
});

it('replaces an accepted suggestion with its Action in the same history entry', async () => {
  renderPage(['/history?item=chat', '/history?item=suggestion:S1']);
  await userEvent.click(screen.getByRole('button', { name: 'accept' }));
  expect(location()).toHaveTextContent('/history?item=action:A9');
  expect(screen.getByRole('region', { name: 'action A9' })).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: 'back' }));
  expect(location()).toHaveTextContent('/history?item=chat');
});

it('keeps the file open beside a suggestion open in its Action, until the selection moves', async () => {
  withoutTodoList();
  renderPage(['/history?item=suggestion:S1']);
  await userEvent.click(screen.getByRole('button', { name: 'reply with a file open' }));
  expect(location()).toHaveTextContent('/history?item=action:A9');
  expect(screen.getByRole('region', { name: 'action A9' })).toHaveTextContent(
    'preview drafts/reply.md'
  );

  // Shown again later, the Action opens as the user left it.
  await userEvent.click(await screen.findByRole('button', { name: /^見積書を直す/ }));
  await userEvent.click(screen.getByRole('button', { name: 'back' }));
  expect(location()).toHaveTextContent('/history?item=action:A9');
  expect(screen.getByRole('region', { name: 'action A9' })).not.toHaveTextContent('preview');
});

it('drops the handed-over file when the address moves away without a selection', async () => {
  renderPage(['/history?item=chat', '/history?item=suggestion:S1']);
  await userEvent.click(screen.getByRole('button', { name: 'reply with a file open' }));
  expect(screen.getByRole('region', { name: 'action A9' })).toHaveTextContent(
    'preview drafts/reply.md'
  );

  await userEvent.click(screen.getByRole('button', { name: 'back' }));
  expect(location()).toHaveTextContent('/history?item=chat');
  await userEvent.click(screen.getByRole('button', { name: 'forward' }));
  expect(location()).toHaveTextContent('/history?item=action:A9');
  expect(screen.getByRole('region', { name: 'action A9' })).not.toHaveTextContent('preview');
});

it('does not hand the file over again when the suggestion is shown after its Action', async () => {
  withoutTodoList();
  renderPage(['/history?item=suggestion:S1']);
  await userEvent.click(screen.getByRole('button', { name: 'reply with a file open' }));
  expect(screen.getByRole('region', { name: 'action A9' })).toHaveTextContent(
    'preview drafts/reply.md'
  );

  // A started suggestion shown again goes straight back to its Action.
  await userEvent.click(await screen.findByRole('button', { name: /^請求書を作りましょうか/ }));
  await userEvent.click(screen.getByRole('button', { name: 'accept' }));
  expect(location()).toHaveTextContent('/history?item=action:A9');
  expect(screen.getByRole('region', { name: 'action A9' })).not.toHaveTextContent('preview');
});

it('opens a new task from ✎ and replaces it with the Action its send opened', async () => {
  withoutTodoList();
  renderPage(['/history?item=chat']);
  await userEvent.click(await screen.findByRole('button', { name: NEW_TASK }));
  expect(location()).toHaveTextContent('/history?item=new');
  expect(parseHistorySelection('?item=new')).toBe('new');
  await userEvent.click(screen.getByRole('button', { name: 'send' }));
  expect(location()).toHaveTextContent('/history?item=action:A7');
});

it('moves the selection off a deleted task in place of its history entry', async () => {
  withoutTodoList();
  renderPage(['/history?item=chat', '/history?item=action:A1']);
  await userEvent.click(await screen.findByRole('button', { name: '削除 見積書を直す' }));
  await userEvent.click(screen.getByRole('button', { name: '削除' }));
  expect(deleteItem).toHaveBeenCalledWith({ kind: 'conversation', id: 'A1' });
  expect(location()).toHaveTextContent('/history?item=suggestion:S1');

  await userEvent.click(screen.getByRole('button', { name: 'back' }));
  expect(location()).toHaveTextContent('/history?item=chat');
});

it("opens a task's sent work chip in place, as the chat's", async () => {
  renderPage(['/history?item=chat', '/history?item=action:A1']);
  await userEvent.click(screen.getByRole('link', { name: '「請求書を作りましょうか」を開く' }));
  expect(location()).toHaveTextContent('/history?item=suggestion:S1');
  expect(screen.getByRole('region', { name: 'suggestion S1' })).toBeInTheDocument();
});

const PROJECT_NAME = /^(プロジェクト名|Project name)$/u;
const NEW_TASK = /^(メイン画面で新しい作業|New task in the main window)$/u;

it('the Action pane starts naming a project in place', async () => {
  renderPage(['/history?item=action:A1']);
  await userEvent.click(screen.getByRole('button', { name: 'add project' }));
  expect(screen.getByRole('textbox', { name: PROJECT_NAME })).toHaveFocus();
  expect(selectFolder).not.toHaveBeenCalled();
  expect(location()).toHaveTextContent('/history?item=action:A1');
});

it('starts naming a project when the Overlay asks for a new one', async () => {
  renderPage(['/history?project=new']);
  expect(await screen.findByRole('textbox', { name: PROJECT_NAME })).toHaveFocus();
  expect(location()).not.toHaveTextContent('project=new');
});

it('writes the selection as `?item=kind:id`, encoding only the id', () => {
  expect(historySelectionSearch('chat')).toBe('?item=chat');
  expect(historySelectionSearch('action:a b&c:d')).toBe('?item=action:a%20b%26c%3Ad');
  expect(parseHistorySelection(historySelectionSearch('action:a b&c:d'))).toBe('action:a b&c:d');
});

const SIDEBAR = { name: 'TODO' } as const;
const HISTORY_SIDEBAR = { name: 'チャットと作業' } as const;

it('hides the sidebar and shows it again, keeping the detail pane and the choice', async () => {
  renderPage(['/history?item=action:A1']);
  const toggle = screen.getByRole('button', { name: 'サイドバーを隠す' });
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  expect(toggle).toHaveAttribute('aria-controls', screen.getByRole('complementary', SIDEBAR).id);

  await userEvent.click(toggle);
  expect(screen.queryByRole('complementary', SIDEBAR)).toBeNull();
  expect(screen.getByRole('region', { name: 'action A1' })).toBeInTheDocument();
  expect(toggle).toHaveAccessibleName('サイドバーを表示');
  expect(toggle).toHaveAttribute('aria-expanded', 'false');

  // The choice survives a restart.
  cleanup();
  renderPage(['/history?item=action:A1']);
  expect(screen.queryByRole('complementary', SIDEBAR)).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'サイドバーを表示' }));
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
  expect(screen.getByRole('region', { name: 'action A1' })).toBeInTheDocument();
});

it('⌘B hides the sidebar and moves focus out of it to the toggle', async () => {
  withoutTodoList();
  renderPage(['/history']);
  const search = screen.getByRole('searchbox', { name: '作業を検索' });
  search.focus();

  fireEvent.keyDown(search, { key: 'b', metaKey: true });
  expect(screen.queryByRole('complementary', HISTORY_SIDEBAR)).toBeNull();
  expect(screen.getByRole('button', { name: 'サイドバーを表示' })).toHaveFocus();

  fireEvent.keyDown(document.body, { key: 'b', metaKey: true });
  expect(screen.getByRole('complementary', HISTORY_SIDEBAR)).toBeVisible();
});

it('starts with the sidebar shown when storage is unavailable', () => {
  vi.stubGlobal('localStorage', {
    getItem: () => {
      throw new DOMException('blocked', 'SecurityError');
    },
  });
  renderPage(['/history']);
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
});

const projectGroups = () =>
  screen.getAllByRole('region').filter((region) => region.classList.contains('todo-group'));

it('lists the week by project, counts open TODOs in the sidebar, and hands the rail its organizations', async () => {
  renderPage(['/history']);
  expect(await screen.findByRole('checkbox', { name: '完了: TODO T1' })).toBeInTheDocument();
  expect(listWeek).toHaveBeenCalledWith({ scope: 'all', project: 'any' });
  // Xer › 見積もり, then 組織なし › 採用 and 組織なし › プロジェクトなし.
  expect(projectGroups()).toHaveLength(3);
  const sidebar = screen.getByRole('complementary', SIDEBAR);
  // T2 is done: one open TODO in 見積もり, one in 採用.
  expect(within(sidebar).getByRole('button', { name: '見積もり' })).toHaveAccessibleDescription(
    '未完了 1 件'
  );
  expect(within(sidebar).getByRole('button', { name: 'すべての TODO' })).toHaveAttribute(
    'aria-current',
    'true'
  );
  expect(mocks.publishOrganizations).toHaveBeenLastCalledWith([
    { organization_id: 'O1', display_name: 'Xer' },
  ]);
});

it("opens a TODO's conversation beside the list: its Action, its suggestion without 見送る, or a note", async () => {
  renderPage(['/history']);
  await userEvent.click(await screen.findByRole('button', { name: /^TODO T1/ }));
  expect(location()).toHaveTextContent('/history?todo=T1');
  expect(screen.getByRole('region', { name: 'action A5' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /^TODO T1/ })).toHaveAttribute('aria-current', 'true');
  // The sidebar folds away beside the conversation; its toggle shows it there without saving.
  expect(screen.queryByRole('complementary', SIDEBAR)).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'サイドバーを表示' }));
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
  expect(stored.has('pantaray.history.sidebarHidden')).toBe(false);

  await userEvent.click(screen.getByRole('button', { name: /^TODO T3/ }));
  const suggestion = screen.getByRole('region', { name: 'suggestion S5' });
  expect(within(suggestion).queryByRole('button', { name: 'dismiss' })).toBeNull();
  // Accepting shows its Action at once, before the week names it.
  await userEvent.click(within(suggestion).getByRole('button', { name: 'accept' }));
  expect(screen.getByRole('region', { name: 'action A9' })).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: /^TODO T4/ }));
  expect(
    screen.getByText('まだ会話はありません。Pantaray が始めると、ここに出ます。')
  ).toBeVisible();

  await userEvent.click(screen.getByRole('button', { name: '会話を閉じる' }));
  expect(location()).toHaveTextContent(/^\/history$/);
  expect(screen.getByRole('heading', { level: 1, name: '今週' })).toHaveFocus();
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
});

it('a project selects its TODOs, keeping the scope that holds it and otherwise showing all', async () => {
  renderPage(['/history?scope=none']);
  const sidebar = screen.getByRole('complementary', SIDEBAR);
  await userEvent.click(await within(sidebar).findByRole('button', { name: '採用' }));
  expect(location()).toHaveTextContent('/history?scope=none&project=project:P2');
  expect(projectGroups()).toHaveLength(1);
  expect(within(projectGroups()[0]).getByRole('heading', { level: 3 })).toHaveTextContent('採用');
  expect(within(sidebar).getByRole('button', { name: '採用' })).toHaveAttribute(
    'aria-current',
    'true'
  );

  // 見積もり is Xer's, outside 組織なし.
  await userEvent.click(within(sidebar).getByRole('button', { name: '見積もり' }));
  expect(location()).toHaveTextContent(/^\/history\?project=project:P1$/);

  await userEvent.click(within(sidebar).getByRole('button', { name: '削除した TODO' }));
  expect(location()).toHaveTextContent(/^\/history\?view=deleted$/);
  expect(await screen.findByText('削除した TODO はありません。')).toBeVisible();
});

it('keeps the scope while a conversation is shown, and drops an organization that is gone', async () => {
  renderPage(['/history?scope=organization:O1']);
  await userEvent.click(await screen.findByRole('button', { name: /^TODO T1/ }));
  // A suggestion the TODO's conversation names opens alone, under the scope in view.
  await userEvent.click(screen.getByRole('link', { name: '「請求書を作りましょうか」を開く' }));
  expect(location()).toHaveTextContent('/history?item=suggestion:S1&scope=organization:O1');
  cleanup();

  renderPage(['/history?scope=organization:GONE']);
  await screen.findByRole('button', { name: '見積もり' });
  expect(location()).toHaveTextContent(/^\/history$/);
});

it('「今週へ」 returns to this week and leaves focus on the list heading', async () => {
  listWeek.mockImplementation(async ({ week_start }) =>
    week_start ? { ...WEEK, week: { start: '2026-10-26', end: '2026-11-01' }, items: [] } : WEEK
  );
  renderPage(['/history?week=2026-10-26']);
  expect(
    await screen.findByRole('heading', { level: 1, name: '10月26日 – 11月1日' })
  ).toBeVisible();
  // An ended week takes no TODOs, so it has no add field.
  expect(screen.queryByRole('textbox', { name: /TODO を書く/ })).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: '今週へ' }));
  expect(location()).toHaveTextContent(/^\/history$/);
  expect(screen.getByRole('heading', { level: 1, name: '今週' })).toHaveFocus();
});

it('adds to the project chosen in the add row, and tells a refused write under its row', async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'プロジェクト' }), '採用');
  await userEvent.type(screen.getByRole('textbox', { name: 'TODO を書く' }), '請求書を送る{Enter}');
  expect(createTodo).toHaveBeenCalledWith(
    expect.objectContaining({
      project_id: 'P2',
      week_start: '2026-11-02',
      text: '請求書を送る',
      due: null,
    })
  );

  const estimate = projectGroups()[0];
  await userEvent.click(within(estimate).getByRole('button', { name: '削除: TODO T1' }));
  const alert = await within(estimate).findByRole('alert');
  expect(alert).toHaveTextContent('この TODO の会話が進んでいるあいだは消せません。');
  // Under the row it was made from, before the next one.
  const rows = within(estimate).getAllByRole('listitem');
  expect(rows.findIndex((row) => row.contains(alert))).toBe(1);
});

const NEXT_WEEK = '2026-11-09';
const addFields = () => screen.getAllByRole('textbox', { name: /TODO を書く/ });

it("closes the week's writes while another week is read, so nothing is added to the last one", async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  expect(addFields()[0]).toBeEnabled();
  listWeek.mockImplementation(({ week_start }) =>
    week_start === NEXT_WEEK ? new Promise(() => {}) : Promise.resolve(WEEK)
  );

  await userEvent.click(screen.getByRole('button', { name: '次の週' }));
  expect(location()).toHaveTextContent(`/history?week=${NEXT_WEEK}`);
  // The last week stays on screen, closed: its fields and checks take nothing.
  for (const field of addFields()) expect(field).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: '完了: TODO T1' })).toBeDisabled();
  await userEvent.type(addFields()[0], '請求書を送る{Enter}');
  expect(createTodo).not.toHaveBeenCalled();
  // The week steps from the one selected, not the one still on screen.
  await userEvent.click(screen.getByRole('button', { name: '次の週' }));
  expect(location()).toHaveTextContent('/history?week=2026-11-16');
});

it('keeps the last week closed and says so when the selected week fails to load', async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  listWeek.mockImplementation(async ({ week_start }) => {
    if (week_start === NEXT_WEEK) throw new Error('offline');
    return WEEK;
  });

  await userEvent.click(screen.getByRole('button', { name: '次の週' }));
  expect(
    await screen.findByText('TODO を読み込めませんでした。表示が古いかもしれません。')
  ).toBeVisible();
  for (const field of addFields()) expect(field).toBeDisabled();
});

it('says a list may be out of date when it fails to load again after a read', async () => {
  const DELETED: DeletedTodoView = {
    todo_id: 'D1',
    project_id: 'P1',
    text: '旧版の見積書',
    deleted_by: 'user',
    deleted_at: '2026-11-01T00:00:00.000Z',
    purge_on: '2026-12-01',
  };
  const STALE = 'TODO を読み込めませんでした。表示が古いかもしれません。';
  for (const first of [[], [DELETED]]) {
    listDeleted.mockImplementation(async () => first);
    renderPage(['/history?view=deleted']);
    await screen.findByText(first.length === 0 ? '削除した TODO はありません。' : '旧版の見積書');
    expect(screen.queryByText(STALE)).toBeNull();

    listDeleted.mockRejectedValue(new Error('offline'));
    act(() => todosChanged());
    expect(await screen.findByRole('alert')).toHaveTextContent(STALE);
    // What was read stays under the note.
    if (first.length > 0) expect(screen.getByText('旧版の見積書')).toBeVisible();
    cleanup();
  }

  // The week's list says the same over the TODOs it last read.
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  listWeek.mockRejectedValue(new Error('offline'));
  act(() => todosChanged());
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(STALE));
  expect(screen.getByRole('checkbox', { name: '完了: TODO T1' })).toBeVisible();
});
