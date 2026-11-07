import { useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type {
  TodoGoalRequest,
  TodoRefusalCode,
  TodoWriteResult,
} from '../../electron/src/todos/todoContracts';
import { todoRefusalMessage } from './useTodoMutations';
import type { TodosBridge } from './useTodoRead';

/** What the goal form sends; the project is the one the form was opened for. */
export type GoalInput = Omit<TodoGoalRequest, 'project_id'>;

/** Where a failed write is told: in the project's goal form, or next to the record. */
export type ProgressMutationTarget =
  | { kind: 'goal'; projectId: string }
  | { kind: 'record'; recordId: string };

const progressRefusalMessage = (code: TodoRefusalCode): MessageKey =>
  code === 'TODO_GOAL_DEADLINE_BEFORE_SET'
    ? 'todoProgress.error.deadlineBeforeSet'
    : todoRefusalMessage(code);

/**
 * The goal and progress-record writes. As with the TODOs' writes, nothing changes ahead of the
 * backend: a write resolves true once it went through and the progress has been read again.
 */
export function useTodoProgressMutations({
  reloadProgress,
}: {
  /** `useTodoProgress`'s `reload`. */
  reloadProgress: () => Promise<void>;
}) {
  const { t } = useI18n();
  const [pendingRecordIds, setPendingRecordIds] = useState<ReadonlySet<string>>(new Set());
  const [failure, setFailure] = useState<{
    target: ProgressMutationTarget;
    key: MessageKey;
  } | null>(null);

  const write = async <T>(
    target: ProgressMutationTarget,
    request: (todos: TodosBridge) => Promise<TodoWriteResult<T>>
  ): Promise<boolean> => {
    setFailure(null);
    try {
      const todos = window.electron?.todos;
      if (!todos) throw new Error('TODO bridge is unavailable.');
      const result = await request(todos);
      if (result.kind === 'refused') {
        setFailure({ target, key: progressRefusalMessage(result.code) });
        return false;
      }
    } catch {
      setFailure({ target, key: 'todo.error.generic' });
      return false;
    }
    await reloadProgress();
    return true;
  };

  const deleteRecord = async (recordId: string): Promise<boolean> => {
    setPendingRecordIds((current) => new Set(current).add(recordId));
    try {
      return await write({ kind: 'record', recordId }, async (todos) => {
        // The backend never refuses it: a record already gone is deleted too.
        await todos.deleteProgressRecord({ record_id: recordId });
        return { kind: 'ok', result: null };
      });
    } finally {
      setPendingRecordIds((current) => {
        const next = new Set(current);
        next.delete(recordId);
        return next;
      });
    }
  };

  return {
    /** The records whose deletion, or the read showing it, has not answered yet. */
    pendingRecordIds,
    failure: failure ? { target: failure.target, message: t(failure.key) } : null,
    setGoal: (projectId: string, goal: GoalInput) =>
      write({ kind: 'goal', projectId }, (todos) =>
        todos.setGoal({ project_id: projectId, ...goal })
      ),
    clearGoal: (projectId: string) =>
      write({ kind: 'goal', projectId }, (todos) => todos.clearGoal({ project_id: projectId })),
    deleteRecord,
  };
}
