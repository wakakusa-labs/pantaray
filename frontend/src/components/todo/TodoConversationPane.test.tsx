import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import { hook } from '@/components/schedules/scheduleFixtures';
import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { HookList } from '../../../electron/src/settings/hooksFetch';
import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { TodoConversationPane } from './TodoConversationPane';

// The conversation itself is the task pane's; only what it opens with is drawn here.
vi.mock('@/components/task/TaskWorkspace', () => ({
  TaskWorkspace: ({ conversationStart }: { conversationStart?: ReactNode }) => (
    <section aria-label="conversation">{conversationStart}</section>
  ),
}));

afterEach(() => {
  cleanup();
  delete window.electron;
});

const scheduled = (overrides: Partial<TodoView> = {}): TodoView => ({
  todo_id: 'todo-fired',
  project_id: null,
  organization_id: null,
  text: '会議メモ',
  author: 'ai',
  origin: { kind: 'hook', origin_action_id: 'action-asked' },
  starts_on: '2026-11-27',
  due: null,
  state: { kind: 'working', note: null },
  deletion_request: null,
  conversation: { kind: 'action', action_id: 'action-fired' },
  ...overrides,
});

function renderPane(todo: TodoView, list: HookList | Error) {
  const hooks = {
    list: vi.fn(async () => {
      if (list instanceof Error) throw list;
      return list;
    }),
  };
  window.electron = { hooks } as unknown as Window['electron'];
  const openWork = vi.fn();
  render(
    <UiLanguageProvider initialLanguage="ja">
      <OpenWorkContext.Provider value={openWork}>
        <TodoConversationPane
          todo={todo}
          pending={false}
          deletionFailure={null}
          onAnswerDeletion={vi.fn()}
          onAddProject={vi.fn()}
          requestedFile={null}
          handedPreview={null}
          pageControls={null}
        />
      </OpenWorkContext.Provider>
    </UiLanguageProvider>
  );
  return { hooks, openWork };
}

// The asking conversation's schedule starts a new conversation each time; another
// conversation's schedule is listed too, under its own title.
const LIST: HookList = {
  hooks: [
    hook({
      hook_id: 'task.other',
      created_from_action_id: 'action-other',
      created_from_title: '別件',
    }),
    hook({
      hook_id: 'task.meeting',
      created_from_action_id: 'action-asked',
      created_from_title: '会議の段取り',
      target_action_id: null,
      title: '会議メモ',
    }),
  ],
  recording_ready: true,
};

// Catches a line that names another conversation, or opens the wrong one.
it('names the conversation a schedule was asked for in, and opens that conversation', async () => {
  const { openWork } = renderPane(scheduled(), LIST);
  const open = await screen.findByRole('button', { name: '「会議の段取り」を開く' });
  expect(open.closest('p')).toHaveTextContent('「会議の段取り」で頼んだ予定で始まりました');
  await userEvent.click(open);
  expect(openWork).toHaveBeenCalledExactlyOnceWith('action:action-asked');
});

// Catches a name or a dead link once the asking conversation, and its schedules, are gone.
it('says only that a schedule started it once the asking conversation is gone', async () => {
  renderPane(scheduled(), { hooks: [LIST.hooks[0]], recording_ready: true });
  expect(await screen.findByText('会話で頼んだ予定で始まりました')).toBeInTheDocument();
  expect(screen.queryByRole('button')).toBeNull();
});

// Catches the line on a conversation no schedule started, and a read made for it.
it('shows no line on a conversation the user or a suggestion started', async () => {
  for (const origin of [{ kind: 'written' }, { kind: 'suggestion' }] as const) {
    cleanup();
    const { hooks } = renderPane(scheduled({ origin }), LIST);
    expect(await screen.findByRole('region', { name: 'conversation' })).toBeEmptyDOMElement();
    expect(hooks.list).not.toHaveBeenCalled();
  }
});
