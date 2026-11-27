import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { hook } from '@/components/schedules/scheduleFixtures';
import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { HookList } from '../../../electron/src/settings/hooksFetch';
import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { useTodoDeleteConfirmation } from './useTodoDeleteConfirmation';

afterEach(() => {
  cleanup();
  delete window.electron;
});

const todo = (conversation: TodoView['conversation']): TodoView => ({
  todo_id: 'todo-1',
  project_id: null,
  organization_id: null,
  text: '発注チェック',
  author: 'user',
  origin: { kind: 'written' },
  starts_on: '2026-11-27',
  due: null,
  state: { kind: 'working', note: null },
  deletion_request: null,
  conversation,
});

const IN_ACTION = todo({ kind: 'action', action_id: 'action-1' });

function Harness({
  item,
  deleteTodo,
  onDeleted,
}: {
  item: TodoView;
  deleteTodo: (todoId: string) => Promise<boolean>;
  onDeleted: () => void;
}) {
  const { requestDelete, dialog } = useTodoDeleteConfirmation({ deleteTodo, onDeleted });
  return (
    <>
      <button type="button" onClick={() => void requestDelete(item)}>
        削除: {item.text}
      </button>
      {dialog}
    </>
  );
}

function renderHarness(item: TodoView, list: HookList | Error) {
  const hooks = {
    list: vi.fn(async () => {
      if (list instanceof Error) throw list;
      return list;
    }),
  };
  window.electron = { hooks } as unknown as Window['electron'];
  const deleteTodo = vi.fn(async () => true);
  const onDeleted = vi.fn();
  render(
    <UiLanguageProvider initialLanguage="ja">
      <Harness item={item} deleteTodo={deleteTodo} onDeleted={onDeleted} />
    </UiLanguageProvider>
  );
  return { hooks, deleteTodo, onDeleted, trigger: screen.getByRole('button', { name: /^削除/ }) };
}

// Asked for here and continuing it, asked for here and starting a new one each time; one
// asked elsewhere does not go with this TODO.
const LIST: HookList = {
  hooks: [
    hook({ hook_id: 'task.here' }),
    hook({ hook_id: 'task.new', target_action_id: null, title: '会議メモ' }),
    hook({
      hook_id: 'task.other',
      created_from_action_id: 'action-9',
      target_action_id: 'action-9',
    }),
  ],
  recording_ready: true,
};

// Catches a delete that drops the TODO's schedules without asking, or miscounts them.
it('asks before deleting a TODO whose conversation has schedules, and counts them', async () => {
  const { deleteTodo, onDeleted, trigger } = renderHarness(IN_ACTION, LIST);
  await userEvent.click(trigger);
  const dialog = await screen.findByRole('dialog', { name: 'この TODO を削除しますか？' });
  expect(dialog).toHaveAccessibleDescription(
    'この TODO の会話で頼んだ予定 2 件も消えます。TODO を戻しても、予定は戻りません。'
  );
  expect(screen.getByRole('button', { name: 'キャンセル' })).toHaveFocus();

  await userEvent.keyboard('{Enter}');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(deleteTodo).not.toHaveBeenCalled();
  await waitFor(() => expect(trigger).toHaveFocus());

  await userEvent.click(trigger);
  await userEvent.click(await screen.findByRole('button', { name: '削除' }));
  expect(deleteTodo).toHaveBeenCalledExactlyOnceWith('todo-1');
  expect(onDeleted).toHaveBeenCalledOnce();
  expect(screen.queryByRole('dialog')).toBeNull();
});

// Catches a question added to every delete, where nothing else goes with the TODO.
it('deletes at once when the conversation has no schedules, or there is no conversation', async () => {
  const cases: [TodoView, HookList][] = [
    [IN_ACTION, { hooks: [LIST.hooks[2]], recording_ready: true }],
    [todo({ kind: 'suggestion', suggestion_id: 's-1' }), LIST],
    [todo({ kind: 'none' }), LIST],
  ];
  for (const [item, list] of cases) {
    cleanup();
    const { deleteTodo, trigger } = renderHarness(item, list);
    await userEvent.click(trigger);
    await waitFor(() => expect(deleteTodo).toHaveBeenCalledExactlyOnceWith('todo-1'));
    expect(screen.queryByRole('dialog')).toBeNull();
  }
});

// Catches a failed read taken for "no schedules", which would delete them unasked.
it('asks when the schedules cannot be read', async () => {
  const { deleteTodo, trigger } = renderHarness(IN_ACTION, new Error('backend down'));
  await userEvent.click(trigger);
  expect(await screen.findByRole('dialog')).toHaveAccessibleDescription(
    'この TODO の会話で頼んだ予定があれば、それも消えます。TODO を戻しても、予定は戻りません。'
  );
  expect(deleteTodo).not.toHaveBeenCalled();
});
