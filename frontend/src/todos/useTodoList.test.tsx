import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { OrchestrationStatus } from '../../electron/src/orchestration/eventContracts';
import type { TodoListPage, TodoListQuery, TodoView } from '../../electron/src/todos/todoContracts';
import { useDeletedTodos } from './useDeletedTodos';
import { useTodoList } from './useTodoList';
import { useTodoMutations } from './useTodoMutations';

let todoChanged: (() => void) | null;
let historyChanged: (() => void) | null;
let statusChanged: ((status: OrchestrationStatus) => void) | null;
const setCheck = vi.fn();
const list = vi.fn<(query: TodoListQuery) => Promise<TodoListPage>>();
const listDeleted = vi.fn();

/** Pages read on different days, so a test can tell which read landed. */
const listPage = (today: string): TodoListPage => ({ today, items: [] });

beforeEach(() => {
  todoChanged = null;
  historyChanged = null;
  statusChanged = null;
  setCheck.mockReset();
  list.mockReset();
  listDeleted.mockReset();
  window.electron = {
    todos: {
      list,
      listDeleted,
      setCheck,
      onChanged: (callback: () => void) => {
        todoChanged = callback;
        return () => (todoChanged = null);
      },
    },
    history: {
      onChanged: (callback: () => void) => {
        historyChanged = callback;
        return () => (historyChanged = null);
      },
    },
    orchestration: {
      onStatus: (callback: (status: OrchestrationStatus) => void) => {
        statusChanged = callback;
        return () => (statusChanged = null);
      },
    },
  } as unknown as Window['electron'];
});

afterEach(() => vi.restoreAllMocks());

it('reads the list it is asked for, and again once for a burst of TODO and Action changes', async () => {
  list.mockResolvedValue(listPage('2026-11-02'));
  const { result } = renderHook(() =>
    useTodoList({ scope: 'organization', organization_id: 'org-1' }, { project: 'any' }, 'due')
  );
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-02'));
  expect(list).toHaveBeenCalledWith({
    scope: 'organization',
    organization_id: 'org-1',
    project: 'any',
    sort: 'due',
  });

  list.mockResolvedValue(listPage('2026-11-09'));
  act(() => {
    todoChanged?.();
    historyChanged?.();
  });
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-09'));
  expect(list).toHaveBeenCalledTimes(2);

  // An Action's change alone also reads the list again: a row's state follows its conversation.
  act(() => historyChanged?.());
  await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
});

it('ignores an answer to an order no longer asked for', async () => {
  let answerFirst: (page: TodoListPage) => void = () => undefined;
  list.mockReturnValueOnce(new Promise((resolve) => (answerFirst = resolve)));
  list.mockResolvedValueOnce(listPage('2026-11-09'));
  const { result, rerender } = renderHook(
    ({ sort }: { sort: 'added' | 'due' }) =>
      useTodoList({ scope: 'all' }, { project: 'any' }, sort),
    { initialProps: { sort: 'added' } }
  );
  rerender({ sort: 'due' });
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-09'));

  await act(async () => answerFirst(listPage('2026-11-02')));
  expect(result.current.page?.today).toBe('2026-11-09');
});

it('keeps the last list read and reports the failed read', async () => {
  list.mockResolvedValueOnce(listPage('2026-11-02'));
  list.mockRejectedValueOnce(new Error('backend down'));
  const { result } = renderHook(() => useTodoList({ scope: 'all' }, { project: 'any' }, 'added'));
  await waitFor(() => expect(result.current.page).not.toBeNull());

  await act(() => result.current.reload());
  expect(result.current.error).toBe(true);
  expect(result.current.page?.today).toBe('2026-11-02');
});

it('reads the deleted TODOs again when the TODOs change', async () => {
  listDeleted.mockResolvedValueOnce([]);
  listDeleted.mockResolvedValueOnce([{ todo_id: 't1' }]);
  const { result } = renderHook(() => useDeletedTodos({ scope: 'no_organization' }));
  await waitFor(() => expect(result.current.items).toEqual([]));
  expect(listDeleted).toHaveBeenCalledWith({ scope: 'no_organization' });

  act(() => todoChanged?.());
  await waitFor(() => expect(result.current.items).toEqual([{ todo_id: 't1' }]));
});

it('reads again when a new WS session starts, since changes made while disconnected are not replayed', async () => {
  list.mockResolvedValueOnce(listPage('2026-11-02'));
  const { result } = renderHook(() => useTodoList({ scope: 'all' }, { project: 'any' }, 'added'));
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-02'));

  act(() => statusChanged?.({ status: 'closed' }));
  expect(list).toHaveBeenCalledOnce();
  // The TODOs changed while the session was down; no todos:changed came for it.
  list.mockResolvedValueOnce(listPage('2026-11-09'));
  act(() => statusChanged?.({ status: 'session_started' }));
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-09'));
});

const todo = (done: boolean): TodoView => ({
  todo_id: 't1',
  project_id: null,
  text: 't1',
  author: 'user',
  origin: 'written',
  starts_on: '2026-11-02',
  due: null,
  state: done
    ? { kind: 'done', by: 'user', at: '2026-11-03T00:00:00Z' }
    : { kind: 'not_started', reason: 'starting' },
  deletion_request: null,
  conversation: { kind: 'none' },
});

it('keeps a checked row pending through a read that a change notification superseded', async () => {
  const pageWith = (done: boolean) => ({ ...listPage('2026-11-02'), items: [todo(done)] });
  list.mockResolvedValueOnce(pageWith(false));
  setCheck.mockResolvedValueOnce({ kind: 'ok', result: {} });
  let answerAfterWrite: (page: TodoListPage) => void = () => undefined;
  let answerAfterChange: (page: TodoListPage) => void = () => undefined;
  list.mockReturnValueOnce(new Promise((resolve) => (answerAfterWrite = resolve)));
  list.mockReturnValueOnce(new Promise((resolve) => (answerAfterChange = resolve)));
  const { result } = renderHook(
    () => {
      const todoList = useTodoList({ scope: 'all' }, { project: 'any' }, 'added');
      const mutations = useTodoMutations({
        reloadList: todoList.reload,
        reloadDeleted: async () => undefined,
      });
      return { todoList, mutations };
    },
    {
      wrapper: ({ children }: { children: ReactNode }) => (
        <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
      ),
    }
  );
  await waitFor(() => expect(result.current.todoList.page).not.toBeNull());

  let checked: Promise<boolean> = Promise.resolve(false);
  await act(async () => {
    checked = result.current.mutations.setCheck('t1', true);
  });
  act(() => todoChanged?.());
  await waitFor(() => expect(list).toHaveBeenCalledTimes(3));

  // The read after the write answers last-but-one: it is dropped, and the row still waits.
  await act(async () => answerAfterWrite(pageWith(true)));
  expect(result.current.mutations.pending.has('t1')).toBe(true);
  expect(result.current.todoList.page?.items[0].state.kind).toBe('not_started');

  await act(async () => answerAfterChange(pageWith(true)));
  expect(await checked).toBe(true);
  expect(result.current.mutations.pending.size).toBe(0);
  expect(result.current.todoList.page?.items[0].state.kind).toBe('done');
});
