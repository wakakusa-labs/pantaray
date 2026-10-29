import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { forwardRef, useImperativeHandle } from 'react';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ActionConversationUserItem } from '../../electron/src/actions/actionConversationModel';
import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import { UserItem } from '@/components/action-conversation/UserMessage';
import type { ChatViewHandle } from '@/components/chat/ChatView';
import type { TaskPreview } from '@/components/task/taskFiles';
import { ChatSessionContext, type ChatSession } from '@/components/chat/chatSession';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { historySelectionSearch, parseHistorySelection } from '@/history/historySelection';
import SuggestionHistoryPage from './SuggestionHistoryPage';

const mocks = vi.hoisted(() => ({ showNewest: vi.fn() }));

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
    onStarted,
  }: {
    suggestionId: string;
    title: string;
    onStarted: (actionId: string, preview: TaskPreview | null) => void;
  }) => (
    <section aria-label={`suggestion ${suggestionId}`}>
      <h1>{title}</h1>
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
const deleteItem = vi.fn(async () => ({ ok: true }) as const);
const selectFolder = vi.fn(async () => ({ canceled: true, path: null }));

const stored = new Map<string, string>();

beforeEach(() => {
  stored.clear();
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
    workspaceSettings: {
      get: vi.fn(async () => ({
        read_access_scope: 'workspace',
        organizations: [],
        projects: [],
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
              <Route path="/history" element={<SuggestionHistoryPage />} />
            </Routes>
            <LocationProbe />
          </ChatSessionContext.Provider>
        </LocalOwnerContext.Provider>
      </UiLanguageProvider>
    </MemoryRouter>
  );

const location = () => screen.getByRole('status', { name: 'location' });

it('shows what `?item=` names, and the chat for a missing or malformed item', async () => {
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

  for (const entry of [
    '/history',
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

it('selects a row into the URL and marks it current; the Chat row of the chat shows the newest', async () => {
  renderPage(['/history']);
  const row = await screen.findByRole('button', { name: /^見積書を直す/ });
  const chatRow = screen.getByRole('button', { name: 'チャット' });
  expect(chatRow).toHaveAttribute('aria-current', 'true');

  await userEvent.click(chatRow);
  expect(mocks.showNewest).toHaveBeenCalledOnce();
  expect(location()).toHaveTextContent(/^\/history$/);

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

it('opens a new task from ✎ and replaces it with the Action its send opened', async () => {
  renderPage(['/history?item=chat']);
  await userEvent.click(await screen.findByRole('button', { name: NEW_TASK }));
  expect(location()).toHaveTextContent('/history?item=new');
  expect(parseHistorySelection('?item=new')).toBe('new');
  await userEvent.click(screen.getByRole('button', { name: 'send' }));
  expect(location()).toHaveTextContent('/history?item=action:A7');
});

it('moves the selection off a deleted task in place of its history entry', async () => {
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

const SIDEBAR = { name: 'チャットと作業' } as const;

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
  renderPage(['/history']);
  const search = screen.getByRole('searchbox', { name: '作業を検索' });
  search.focus();

  fireEvent.keyDown(search, { key: 'b', metaKey: true });
  expect(screen.queryByRole('complementary', SIDEBAR)).toBeNull();
  expect(screen.getByRole('button', { name: 'サイドバーを表示' })).toHaveFocus();

  fireEvent.keyDown(document.body, { key: 'b', metaKey: true });
  expect(screen.getByRole('complementary', SIDEBAR)).toBeVisible();
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
