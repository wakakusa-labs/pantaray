import { useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type {
  TodoDue,
  TodoRefusalCode,
  TodoWriteResult,
} from '../../electron/src/todos/todoContracts';
import type { TodosBridge } from './useTodoRead';

/**
 * Where a failed write is told: next to its row, under the add row, or under the deletion
 * request in the TODO's conversation, which stays in view when a narrow window hides the list.
 */
export type TodoMutationTarget =
  | { kind: 'todo'; todoId: string }
  | { kind: 'add' }
  | { kind: 'deletion'; todoId: string };

export type TodoMutationFailure = { target: TodoMutationTarget; message: string };

/**
 * The refusals the user can do something about, or that explain what they see. The rest (a TODO
 * gone or changed meanwhile, which the next read shows, or the system's own reasons) read as
 * the generic message.
 */
const REFUSAL_MESSAGES: Partial<Record<TodoRefusalCode, MessageKey>> = {
  TODO_BUSY: 'todo.error.busy',
  TODO_RESTORE_EXPIRED: 'todo.error.restoreExpired',
  PROJECT_NOT_FOUND: 'todo.error.projectNotFound',
};

export const todoRefusalMessage = (code: TodoRefusalCode): MessageKey =>
  REFUSAL_MESSAGES[code] ?? 'todo.error.generic';

type Outcome = 'ok' | 'refused' | 'unknown';

const addKey = (projectId: string | null, weekStart: string) => `${weekStart}\n${projectId ?? ''}`;

/** The reads that show a write's result: `useTodoWeek`'s and `useDeletedTodos`'s `reload`. */
export type TodoReloads = {
  reloadWeek: () => Promise<void>;
  reloadDeleted: () => Promise<void>;
};

/**
 * The TODO list's writes. Nothing is changed ahead of the backend: a TODO with a write in flight
 * stays pending until the write has answered and the lists showing its result have been read
 * again, so its controls never act on the state from before the write. The latest failed write
 * is told next to where it was made.
 */
export function useTodoMutations({ reloadWeek, reloadDeleted }: TodoReloads) {
  const { t } = useI18n();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [failure, setFailure] = useState<{ target: TodoMutationTarget; key: MessageKey } | null>(
    null
  );
  // An add whose answer never came may have landed, so sending the same line again reuses its
  // `client_todo_id` and the backend returns that TODO instead of adding a second one.
  const unansweredAddsRef = useRef(
    new Map<string, { text: string; due: string | null; clientTodoId: string }>()
  );

  const send = async <T>(
    target: TodoMutationTarget,
    request: (todos: TodosBridge) => Promise<TodoWriteResult<T>>
  ): Promise<Outcome> => {
    setFailure(null);
    try {
      const todos = window.electron?.todos;
      if (!todos) throw new Error('TODO bridge is unavailable.');
      const result = await request(todos);
      if (result.kind === 'ok') return 'ok';
      setFailure({ target, key: todoRefusalMessage(result.code) });
      return 'refused';
    } catch {
      setFailure({ target, key: 'todo.error.generic' });
      return 'unknown';
    }
  };

  const reloadBoth = () => Promise.all([reloadWeek(), reloadDeleted()]);

  const writeRow = async <T>(
    todoId: string,
    request: (todos: TodosBridge) => Promise<TodoWriteResult<T>>,
    reload: () => Promise<unknown>,
    target: TodoMutationTarget = { kind: 'todo', todoId }
  ): Promise<boolean> => {
    setPending((current) => new Set(current).add(todoId));
    try {
      // A refused or unanswered write changed nothing the screen shows; a change still reads
      // again on `todos:changed`.
      if ((await send(target, request)) !== 'ok') return false;
      await reload();
      return true;
    } finally {
      setPending((current) => {
        const next = new Set(current);
        next.delete(todoId);
        return next;
      });
    }
  };

  /** Resolves true once the added TODO has been read into the week, which empties the add field. */
  const create = async (
    projectId: string | null,
    weekStart: string,
    text: string,
    due: TodoDue | null
  ): Promise<boolean> => {
    // Enter on an empty field adds nothing and tells nothing.
    if (!text.trim()) return false;
    const key = addKey(projectId, weekStart);
    const unanswered = unansweredAddsRef.current.get(key);
    const dueKey = due ? `${due.date}:${due.repeat}` : null;
    const clientTodoId =
      unanswered?.text === text && unanswered.due === dueKey
        ? unanswered.clientTodoId
        : crypto.randomUUID();
    unansweredAddsRef.current.set(key, { text, due: dueKey, clientTodoId });
    const outcome = await send({ kind: 'add' }, (todos) =>
      todos.create({
        client_todo_id: clientTodoId,
        project_id: projectId,
        text,
        week_start: weekStart,
        due,
      })
    );
    if (outcome !== 'unknown') unansweredAddsRef.current.delete(key);
    if (outcome !== 'ok') return false;
    await reloadWeek();
    return true;
  };

  return {
    /** The TODOs whose write, or the read showing it, has not answered yet. */
    pending,
    failure: failure ? { target: failure.target, message: t(failure.key) } : null,
    create,
    setCheck: (todoId: string, checked: boolean) =>
      writeRow(todoId, (todos) => todos.setCheck({ todo_id: todoId, checked }), reloadWeek),
    setDue: (todoId: string, due: TodoDue | null) =>
      writeRow(todoId, (todos) => todos.setDue({ todo_id: todoId, due }), reloadWeek),
    // Deleting and restoring move the TODO between the week and the deleted list.
    deleteTodo: (todoId: string) =>
      writeRow(todoId, (todos) => todos.delete({ todo_id: todoId }), reloadBoth),
    answerDeletion: (todoId: string, requestId: string, answer: 'approve' | 'keep') =>
      writeRow(
        todoId,
        (todos) => todos.answerDeletion({ todo_id: todoId, request_id: requestId, answer }),
        reloadWeek,
        { kind: 'deletion', todoId }
      ),
    restore: (todoId: string) =>
      writeRow(todoId, (todos) => todos.restore({ todo_id: todoId }), reloadBoth),
  };
}
