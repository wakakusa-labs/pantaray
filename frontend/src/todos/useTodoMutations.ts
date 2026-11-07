import { useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type {
  TodoDue,
  TodoRefusalCode,
  TodoWriteResult,
} from '../../electron/src/todos/todoContracts';
import type { TodosBridge } from './useTodoRead';

/** Where a failed write is told: next to its row, or next to the group's add field. */
export type TodoMutationTarget =
  | { kind: 'todo'; todoId: string }
  | { kind: 'add'; projectId: string | null };

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

/**
 * The TODO list's writes. Each TODO with a write in flight is pending until its answer comes;
 * nothing is changed ahead of it, and the week's next read shows the result. The latest failed
 * write is told next to where it was made.
 */
export function useTodoMutations() {
  const { t } = useI18n();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [failure, setFailure] = useState<{ target: TodoMutationTarget; key: MessageKey } | null>(
    null
  );
  // An add whose answer never came may have landed, so sending the same line again reuses its
  // `client_todo_id` and the backend returns that TODO instead of adding a second one.
  const unansweredAddsRef = useRef(new Map<string, { text: string; clientTodoId: string }>());

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

  const writeRow = async <T>(
    todoId: string,
    request: (todos: TodosBridge) => Promise<TodoWriteResult<T>>
  ): Promise<boolean> => {
    setPending((current) => new Set(current).add(todoId));
    try {
      return (await send({ kind: 'todo', todoId }, request)) === 'ok';
    } finally {
      setPending((current) => {
        const next = new Set(current);
        next.delete(todoId);
        return next;
      });
    }
  };

  /** Resolves true when the TODO was added, which empties the add field. */
  const create = async (
    projectId: string | null,
    weekStart: string,
    text: string
  ): Promise<boolean> => {
    // Enter on an empty field adds nothing and tells nothing.
    if (!text.trim()) return false;
    const key = addKey(projectId, weekStart);
    const unanswered = unansweredAddsRef.current.get(key);
    const clientTodoId = unanswered?.text === text ? unanswered.clientTodoId : crypto.randomUUID();
    unansweredAddsRef.current.set(key, { text, clientTodoId });
    const outcome = await send({ kind: 'add', projectId }, (todos) =>
      todos.create({
        client_todo_id: clientTodoId,
        project_id: projectId,
        text,
        week_start: weekStart,
        due: null,
      })
    );
    if (outcome !== 'unknown') unansweredAddsRef.current.delete(key);
    return outcome === 'ok';
  };

  return {
    /** The TODOs whose write has not answered yet. */
    pending,
    failure: failure ? { target: failure.target, message: t(failure.key) } : null,
    create,
    setCheck: (todoId: string, checked: boolean) =>
      writeRow(todoId, (todos) => todos.setCheck({ todo_id: todoId, checked })),
    setDue: (todoId: string, due: TodoDue | null) =>
      writeRow(todoId, (todos) => todos.setDue({ todo_id: todoId, due })),
    deleteTodo: (todoId: string) => writeRow(todoId, (todos) => todos.delete({ todo_id: todoId })),
    answerDeletion: (todoId: string, requestId: string, answer: 'approve' | 'keep') =>
      writeRow(todoId, (todos) =>
        todos.answerDeletion({ todo_id: todoId, request_id: requestId, answer })
      ),
    restore: (todoId: string) => writeRow(todoId, (todos) => todos.restore({ todo_id: todoId })),
  };
}
