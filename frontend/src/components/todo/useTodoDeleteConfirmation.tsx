import { useEffect, useRef, useState, type ReactNode } from 'react';

import { HistoryDeleteDialog } from '@/components/history/HistoryDeleteDialog';
import { useI18n } from '@/context/useI18n';

import type { TodoView } from '../../../electron/src/todos/todoContracts';

/** How many schedules go with the TODO, or `unknown` when the list could not be read. */
type ScheduleCount = number | 'unknown';

/**
 * The schedules asked for in the TODO's conversation and those sending to it, which deleting the
 * TODO deletes too. Only an Action's conversation can hold schedules.
 */
async function countSchedules(todo: TodoView): Promise<ScheduleCount> {
  const { conversation } = todo;
  if (conversation.kind !== 'action') return 0;
  const actionId = conversation.action_id;
  try {
    const hooks = window.electron?.hooks;
    if (!hooks) throw new Error('Schedules bridge is unavailable.');
    const list = await hooks.list();
    return list.hooks.filter(
      (hook) => hook.created_from_action_id === actionId || hook.target_action_id === actionId
    ).length;
  } catch {
    return 'unknown';
  }
}

/**
 * Deleting a TODO deletes the schedules of its conversation, and restoring the TODO does not
 * bring them back, so a TODO with schedules is deleted only once the user agrees; one with none
 * is deleted at once, as before. A count that cannot be read asks too, since what goes is not
 * known. Cancelling returns the focus to the delete button; deleting hands it to `onDeleted`,
 * since the row goes.
 */
export function useTodoDeleteConfirmation({
  deleteTodo,
  onDeleted,
}: {
  deleteTodo: (todoId: string) => Promise<boolean>;
  onDeleted: () => void;
}): { requestDelete: (todo: TodoView) => Promise<void>; dialog: ReactNode } {
  const { t } = useI18n();
  const [asking, setAsking] = useState<{
    todoId: string;
    schedules: ScheduleCount;
    trigger: HTMLElement | null;
  } | null>(null);
  // Focus can leave the modal dialog only once it is gone.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (asking !== null) return;
    returnFocusRef.current?.focus();
    returnFocusRef.current = null;
  }, [asking]);

  const requestDelete = async (todo: TodoView) => {
    // The delete button that was pressed, to return to if the user keeps the TODO.
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const schedules = await countSchedules(todo);
    if (schedules === 0) {
      void deleteTodo(todo.todo_id);
      return;
    }
    setAsking({ todoId: todo.todo_id, schedules, trigger });
  };

  const dialog = asking ? (
    <HistoryDeleteDialog
      t={t}
      title={t('todo.deleteConfirm.title')}
      body={
        asking.schedules === 'unknown'
          ? t('todo.deleteConfirm.maybeSchedules')
          : t(
              asking.schedules === 1
                ? 'todo.deleteConfirm.schedules.one'
                : 'todo.deleteConfirm.schedules.other',
              { count: asking.schedules }
            )
      }
      onCancel={() => {
        returnFocusRef.current = asking.trigger;
        setAsking(null);
      }}
      onConfirm={() => {
        setAsking(null);
        onDeleted();
        void deleteTodo(asking.todoId);
      }}
    />
  ) : null;

  return { requestDelete, dialog };
}
