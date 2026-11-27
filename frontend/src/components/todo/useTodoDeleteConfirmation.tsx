import { useEffect, useRef, useState, type ReactNode } from 'react';

import { HistoryDeleteDialog } from '@/components/history/HistoryDeleteDialog';
import {
  countConversationSchedules,
  type ConversationScheduleCount,
} from '@/components/schedules/scheduleCount';
import { useI18n } from '@/context/useI18n';

import type { TodoView } from '../../../electron/src/todos/todoContracts';

/**
 * Deleting a TODO deletes the schedules of its conversation, and restoring the TODO does not
 * bring them back, so a TODO with schedules is deleted only once the user agrees; one with none
 * is deleted at once, as before. A count that cannot be read asks too, since what goes is not
 * known. A TODO pressed later takes the question over: an earlier count that answers after it
 * opens nothing, so the question always names, and deletes, the TODO pressed last. Cancelling
 * returns the focus to the delete button; deleting hands it to `onDeleted`, since the row goes.
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
    text: string;
    schedules: ConversationScheduleCount;
    trigger: HTMLElement | null;
  } | null>(null);
  // Focus can leave the modal dialog only once it is gone.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (asking !== null) return;
    returnFocusRef.current?.focus();
    returnFocusRef.current = null;
  }, [asking]);

  // Counts can answer out of order; only the last delete pressed may open the question.
  const latestRequestRef = useRef(0);

  const requestDelete = async (todo: TodoView) => {
    const request = ++latestRequestRef.current;
    // The delete button that was pressed, to return to if the user keeps the TODO.
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Only an Action's conversation can hold schedules.
    const { conversation } = todo;
    const schedules =
      conversation.kind === 'action' ? await countConversationSchedules(conversation.action_id) : 0;
    if (schedules === 0) {
      void deleteTodo(todo.todo_id);
      return;
    }
    if (request !== latestRequestRef.current) return;
    setAsking({ todoId: todo.todo_id, text: todo.text, schedules, trigger });
  };

  const dialog = asking ? (
    <HistoryDeleteDialog
      t={t}
      title={t('todo.deleteConfirm.title', { text: asking.text })}
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
