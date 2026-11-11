import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { forwardRef, useImperativeHandle, useState } from 'react';
import { MemoryRouter, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ActionConversationUserItem } from '../../electron/src/actions/actionConversationModel';
import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import type {
  DeletedTodoView,
  TodoListPage,
  TodoListQuery,
  TodoView,
} from '../../electron/src/todos/todoContracts';
import { UserItem } from '@/components/action-conversation/UserMessage';
import type { ChatViewHandle } from '@/components/chat/ChatView';
import type { TaskPreview } from '@/components/task/taskFiles';
import { ChatSessionContext, type ChatSession } from '@/components/chat/chatSession';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { historySelectionSearch, parseHistorySelection } from '@/history/historySelection';
import type { LayoutOutletContext } from '@/components/Layout';
import { ORGANIZATION_ADD_BUTTON_ID } from '@/components/todo/OrganizationRail';
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
    starts_on: '2026-11-02',
    due: null,
    state: { kind: 'working', note: null },
    deletion_request: null,
    conversation: { kind: 'none' },
    ...extra,
  };
}

const PAGE: TodoListPage = {
  today: '2026-11-04',
  items: [
    todo('T1', 'P1', { conversation: { kind: 'action', action_id: 'A5' } }),
    todo('T2', 'P1', { state: { kind: 'done', by: 'user', at: '2026-11-03T01:00:00.000Z' } }),
    todo('T3', 'P2', { conversation: { kind: 'suggestion', suggestion_id: 'S5' } }),
    todo('T4', null),
  ],
};
const list = vi.fn(async (_query: TodoListQuery) => PAGE);
const listDeleted = vi.fn(async (): Promise<DeletedTodoView[]> => []);
/** Every reader that listens for `todos:changed`; the test tells them all, as main does. */
const changeListeners = new Set<() => void>();
const todosChanged = () => changeListeners.forEach((listener) => listener());
const removeTodo = vi.fn(async () => ({ kind: 'refused', code: 'TODO_BUSY' }) as const);
const createTodo = vi.fn(async () => ({ kind: 'ok', result: {} }) as const);
const deleteItem = vi.fn(async () => ({ ok: true }) as const);
const selectFolder = vi.fn(async () => ({ canceled: true, path: null }));
const openFolder = vi.fn(async () => undefined);
const deleteFolder = vi.fn(async () => undefined);
const createOrganization = vi.fn(
  async ({ displayName, iconDataUrl }: { displayName: string; iconDataUrl: string | null }) => ({
    organization_id: 'O2',
    display_name: displayName,
    icon_data_url: iconDataUrl,
  })
);
const updateOrganization = vi.fn(
  async (organizationId: string, input: { displayName: string; iconDataUrl: string | null }) => ({
    organization_id: organizationId,
    display_name: input.displayName,
    icon_data_url: input.iconDataUrl,
  })
);

const stored = new Map<string, string>();

beforeEach(() => {
  mocks.todoUx = true;
  stored.clear();
  list.mockImplementation(async () => PAGE);
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
      list,
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
        folders: [
          {
            folder_id: 'F1',
            display_name: 'estimates',
            real_path: '/Users/me/work/estimates',
            canonical_real_path: '/Users/me/work/estimates',
            organization_ids: [],
            project_ids: ['P1'],
          },
        ],
      })),
      selectFolder,
      openFolder,
      deleteFolder,
      createOrganization,
      updateOrganization,
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

/** The shell's part: the rail's ＋ asks the page to add an organization. */
function Shell() {
  const [creatingOrganization, setCreatingOrganization] = useState(false);
  const context: LayoutOutletContext = {
    publishOrganizations: mocks.publishOrganizations,
    creatingOrganization,
    onCreatingOrganizationChange: setCreatingOrganization,
  };
  return (
    <>
      <button
        type="button"
        id={ORGANIZATION_ADD_BUTTON_ID}
        onClick={() => setCreatingOrganization(true)}
      >
        rail add
      </button>
      <Outlet context={context} />
    </>
  );
}

const renderPage = (entries: string[]) =>
  render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <UiLanguageProvider initialLanguage="ja">
        <LocalOwnerContext.Provider value={{ id: 'user-1', kind: 'account' }}>
          <ChatSessionContext.Provider value={{} as ChatSession}>
            <Routes>
              <Route element={<Shell />}>
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
  expect(screen.getByRole('heading', { level: 1, name: 'すべての TODO' })).toBeInTheDocument();
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
  expect(screen.queryByRole('heading', { level: 1, name: 'すべての TODO' })).toBeNull();
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

it('lists every TODO by project, counts open TODOs in the sidebar, and hands the rail its organizations', async () => {
  renderPage(['/history']);
  expect(await screen.findByRole('checkbox', { name: '完了: TODO T1' })).toBeInTheDocument();
  expect(list).toHaveBeenCalledWith({ scope: 'all', project: 'any', sort: 'added' });
  // Xer › 見積もり, then 組織なし › 採用 and 組織なし › プロジェクトなし.
  expect(projectGroups()).toHaveLength(3);
  expect(screen.getByText('1 / 4 完了')).toBeVisible();
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
  // Accepting shows its Action at once, before the list names it.
  await userEvent.click(within(suggestion).getByRole('button', { name: 'accept' }));
  expect(screen.getByRole('region', { name: 'action A9' })).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: /^TODO T4/ }));
  expect(
    screen.getByText('まだ会話はありません。Pantaray が始めると、ここに出ます。')
  ).toBeVisible();

  await userEvent.click(screen.getByRole('button', { name: '会話を閉じる' }));
  expect(location()).toHaveTextContent(/^\/history$/);
  expect(screen.getByRole('heading', { level: 1, name: 'すべての TODO' })).toHaveFocus();
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
});

it('the sidebar lists the scope’s projects, and one selects its TODOs within the scope', async () => {
  renderPage(['/history?scope=none']);
  const sidebar = screen.getByRole('complementary', SIDEBAR);
  await userEvent.click(await within(sidebar).findByRole('button', { name: '採用' }));
  expect(location()).toHaveTextContent('/history?scope=none&project=project:P2');
  // One project is one list under its name, with no group heading.
  expect(screen.getByRole('heading', { level: 1, name: '採用' })).toBeVisible();
  expect(projectGroups()).toHaveLength(0);
  const rows = within(screen.getByRole('list', { name: '採用' })).getAllByRole('checkbox');
  expect(rows.map((row) => row.getAttribute('aria-label'))).toEqual(['完了: TODO T3']);
  expect(screen.getByText('0 / 1 完了')).toBeVisible();
  expect(within(sidebar).getByRole('button', { name: '採用' })).toHaveAttribute(
    'aria-current',
    'true'
  );

  // 見積もり is Xer's, outside 組織なし.
  expect(within(sidebar).queryByRole('button', { name: '見積もり' })).toBeNull();

  await userEvent.click(within(sidebar).getByRole('button', { name: '削除した TODO' }));
  expect(location()).toHaveTextContent(/^\/history\?scope=none&view=deleted$/);
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

it('orders the list by due date from the sort switch, and keeps the order in the URL', async () => {
  // T5 was added after T1 and falls due before it; the done T2 follows the open ones either way.
  const [t1, t2, ...rest] = PAGE.items;
  const t5 = todo('T5', 'P1');
  list.mockImplementation(async ({ sort }) => ({
    ...PAGE,
    items: sort === 'due' ? [t5, t1, t2, ...rest] : [t1, t5, t2, ...rest],
  }));
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  const sidebar = screen.getByRole('complementary', SIDEBAR);
  const shownIn = (projectName: string) =>
    within(screen.getByRole('list', { name: projectName }))
      .getAllByRole('checkbox')
      .map((check) => check.getAttribute('aria-label'));
  expect(screen.getByRole('combobox', { name: '並び順' })).toHaveValue('added');

  await userEvent.click(within(sidebar).getByRole('button', { name: '見積もり' }));
  expect(shownIn('見積もり')).toEqual(['完了: TODO T1', '完了: TODO T5', '完了: TODO T2']);
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '並び順' }), '期限順');
  expect(location()).toHaveTextContent('/history?project=project:P1&sort=due');
  expect(list).toHaveBeenLastCalledWith({ scope: 'all', project: 'any', sort: 'due' });
  await waitFor(() =>
    expect(shownIn('見積もり')).toEqual(['完了: TODO T5', '完了: TODO T1', '完了: TODO T2'])
  );

  // Every TODO in view keeps the order chosen.
  await userEvent.click(within(sidebar).getByRole('button', { name: 'すべての TODO' }));
  expect(location()).toHaveTextContent(/^\/history\?sort=due$/);
  expect(screen.getByRole('combobox', { name: '並び順' })).toHaveValue('due');
});

it('adds to the project chosen in the add bar from today, and tells a refused write under its row', async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'プロジェクト' }), '採用');
  await userEvent.type(screen.getByRole('textbox', { name: 'TODO を追加' }), '請求書を送る{Enter}');
  expect(createTodo).toHaveBeenCalledWith(
    expect.objectContaining({
      project_id: 'P2',
      starts_on: '2026-11-04',
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

const addFields = () => screen.getAllByRole('textbox', { name: /TODO を追加/ });

it("closes the list's writes while another order is read", async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  expect(addFields()[0]).toBeEnabled();
  list.mockImplementation(({ sort }) =>
    sort === 'due' ? new Promise(() => {}) : Promise.resolve(PAGE)
  );

  await userEvent.selectOptions(screen.getByRole('combobox', { name: '並び順' }), '期限順');
  // The last page stays on screen, closed: its fields and checks take nothing.
  for (const field of addFields()) expect(field).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: '完了: TODO T1' })).toBeDisabled();
  await userEvent.type(addFields()[0], '請求書を送る{Enter}');
  expect(createTodo).not.toHaveBeenCalled();
});

it('keeps the last page closed and says so when the selected order fails to load', async () => {
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  list.mockImplementation(async ({ sort }) => {
    if (sort === 'due') throw new Error('offline');
    return PAGE;
  });

  await userEvent.selectOptions(screen.getByRole('combobox', { name: '並び順' }), '期限順');
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

  // The TODO list says the same over the TODOs it last read.
  renderPage(['/history']);
  await screen.findByRole('checkbox', { name: '完了: TODO T1' });
  list.mockRejectedValue(new Error('offline'));
  act(() => todosChanged());
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(STALE));
  expect(screen.getByRole('checkbox', { name: '完了: TODO T1' })).toBeVisible();
});

it('the project in view shows its folders under the header, to open in Finder or remove', async () => {
  renderPage(['/history']);
  const sidebar = screen.getByRole('complementary', SIDEBAR);
  expect(screen.queryByRole('list', { name: 'フォルダ' })).toBeNull();
  await userEvent.click(await within(sidebar).findByRole('button', { name: '見積もり' }));

  const folders = screen.getByRole('list', { name: 'フォルダ' });
  expect(within(folders).getByRole('button', { name: '/Users/me/work/estimates' })).toBeVisible();
  await userEvent.click(within(folders).getByRole('button', { name: 'Finder で開く' }));
  expect(openFolder).toHaveBeenCalledWith('F1');
  await userEvent.click(within(folders).getByRole('button', { name: 'estimates を外す' }));
  // Only this project holds it, so it leaves the workspace.
  expect(deleteFolder).toHaveBeenCalledWith('F1');
  await waitFor(() => expect(within(folders).queryByRole('listitem')).toBeNull());
  expect(screen.getByRole('button', { name: 'フォルダを追加' })).toHaveFocus();
});

it('the rail’s ＋ adds an organization, shows it, and returns focus to ＋', async () => {
  renderPage(['/history']);
  await screen.findByRole('button', { name: '見積もり' });
  const add = screen.getByRole('button', { name: 'rail add' });
  await userEvent.click(add);
  const dialog = screen.getByRole('dialog', { name: '組織を追加' });
  const name = within(dialog).getByRole('textbox', { name: '組織の名前' });
  expect(name).toHaveFocus();

  // A name another organization has is refused in place, before anything is sent.
  await userEvent.type(name, 'Xer{Enter}');
  expect(createOrganization).not.toHaveBeenCalled();
  expect(within(dialog).getByRole('alert')).toHaveTextContent(
    'Xer という名前の組織がすでにあります。'
  );
  expect(name).toHaveAttribute('aria-invalid', 'true');

  await userEvent.clear(name);
  await userEvent.type(name, 'Wakakusa{Enter}');
  expect(createOrganization).toHaveBeenCalledWith({ displayName: 'Wakakusa', iconDataUrl: null });
  await waitFor(() => expect(location()).toHaveTextContent('/history?scope=organization:O2'));
  expect(screen.queryByRole('dialog', { name: '組織を追加' })).toBeNull();
  expect(add).toHaveFocus();
  // The project ＋ now makes projects inside it.
  expect(screen.getByRole('heading', { level: 2, name: 'Wakakusa' })).toBeVisible();

  // Escape is the dialog's cancel, which the test environment does not send from the key.
  await userEvent.click(add);
  fireEvent(
    screen.getByRole('dialog', { name: '組織を追加' }),
    new Event('cancel', { cancelable: true })
  );
  expect(screen.queryByRole('dialog', { name: '組織を追加' })).toBeNull();
  expect(add).toHaveFocus();
});

it('the sidebar’s title renames the organization in view, and all organizations hide the project ＋', async () => {
  renderPage(['/history']);
  const sidebar = await screen.findByRole('complementary', SIDEBAR);
  await within(sidebar).findByRole('button', { name: '見積もり' });
  expect(within(sidebar).queryByRole('button', { name: 'プロジェクトを追加' })).toBeNull();
  expect(within(sidebar).queryByRole('button', { name: /を編集$/u })).toBeNull();
  cleanup();

  renderPage(['/history?scope=organization:O1']);
  const edit = await screen.findByRole('button', { name: 'Xer を編集' });
  expect(screen.getByRole('button', { name: 'プロジェクトを追加' })).toBeVisible();
  await userEvent.click(edit);
  const dialog = screen.getByRole('dialog', { name: 'Xer を編集' });
  const name = within(dialog).getByRole('textbox', { name: '組織の名前' });
  await userEvent.clear(name);
  await userEvent.type(name, 'Xer Inc{Enter}');
  expect(updateOrganization).toHaveBeenCalledWith('O1', {
    displayName: 'Xer Inc',
    iconDataUrl: null,
  });
  expect(await screen.findByRole('heading', { level: 2, name: 'Xer Inc' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Xer Inc を編集' })).toHaveFocus();
});
