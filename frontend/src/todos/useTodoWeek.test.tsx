import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { OrchestrationStatus } from '../../electron/src/orchestration/eventContracts';
import type { TodoView, TodoWeekPage, TodoWeekQuery } from '../../electron/src/todos/todoContracts';
import { useDeletedTodos } from './useDeletedTodos';
import { useTodoMutations } from './useTodoMutations';
import { useTodoWeek } from './useTodoWeek';

let todoChanged: (() => void) | null;
let historyChanged: (() => void) | null;
let statusChanged: ((status: OrchestrationStatus) => void) | null;
const setCheck = vi.fn();
const listWeek = vi.fn<(query: TodoWeekQuery) => Promise<TodoWeekPage>>();
const listDeleted = vi.fn();

const weekPage = (start: string): TodoWeekPage => ({
  today: '2026-11-07',
  current_week_start: '2026-11-02',
  week: { start, end: start },
  items: [],
});

beforeEach(() => {
  todoChanged = null;
  historyChanged = null;
  statusChanged = null;
  setCheck.mockReset();
  listWeek.mockReset();
  listDeleted.mockReset();
  window.electron = {
    todos: {
      listWeek,
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

it('reads the week it is asked for, and again once for a burst of TODO and Action changes', async () => {
  listWeek.mockResolvedValue(weekPage('2026-11-02'));
  const { result } = renderHook(() =>
    useTodoWeek({ scope: 'organization', organization_id: 'org-1' }, { project: 'any' }, null)
  );
  await waitFor(() => expect(result.current.page?.week.start).toBe('2026-11-02'));
  expect(listWeek).toHaveBeenCalledWith({
    scope: 'organization',
    organization_id: 'org-1',
    project: 'any',
  });

  listWeek.mockResolvedValue(weekPage('2026-11-09'));
  act(() => {
    todoChanged?.();
    historyChanged?.();
  });
  await waitFor(() => expect(result.current.page?.week.start).toBe('2026-11-09'));
  expect(listWeek).toHaveBeenCalledTimes(2);

  // An Action's change alone also reads the week again: a row's state follows its conversation.
  act(() => historyChanged?.());
  await waitFor(() => expect(listWeek).toHaveBeenCalledTimes(3));
});

it('ignores an answer to a week no longer asked for', async () => {
  let answerFirst: (page: TodoWeekPage) => void = () => undefined;
  listWeek.mockReturnValueOnce(new Promise((resolve) => (answerFirst = resolve)));
  listWeek.mockResolvedValueOnce(weekPage('2026-11-09'));
  const { result, rerender } = renderHook(
    ({ week }: { week: string }) => useTodoWeek({ scope: 'all' }, { project: 'any' }, week),
    { initialProps: { week: '2026-11-02' } }
  );
  rerender({ week: '2026-11-09' });
  await waitFor(() => expect(result.current.page?.week.start).toBe('2026-11-09'));

  await act(async () => answerFirst(weekPage('2026-11-02')));
  expect(result.current.page?.week.start).toBe('2026-11-09');
});

it('keeps the last week read and reports the failed read', async () => {
  listWeek.mockResolvedValueOnce(weekPage('2026-11-02'));
  listWeek.mockRejectedValueOnce(new Error('backend down'));
  const { result } = renderHook(() => useTodoWeek({ scope: 'all' }, { project: 'any' }, null));
  await waitFor(() => expect(result.current.page).not.toBeNull());

  await act(() => result.current.reload());
  expect(result.current.error).toBe(true);
  expect(result.current.page?.week.start).toBe('2026-11-02');
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
  listWeek.mockResolvedValueOnce(weekPage('2026-11-02'));
  const { result } = renderHook(() => useTodoWeek({ scope: 'all' }, { project: 'any' }, null));
  await waitFor(() => expect(result.current.page?.week.start).toBe('2026-11-02'));

  act(() => statusChanged?.({ status: 'closed' }));
  expect(listWeek).toHaveBeenCalledOnce();
  // The TODOs changed while the session was down; no todos:changed came for it.
  listWeek.mockResolvedValueOnce(weekPage('2026-11-09'));
  act(() => statusChanged?.({ status: 'session_started' }));
  await waitFor(() => expect(result.current.page?.week.start).toBe('2026-11-09'));
});

const todo = (done: boolean): TodoView => ({
  todo_id: 't1',
  project_id: null,
  text: 't1',
  author: 'user',
  origin: 'written',
  week_start: '2026-11-02',
  carried: false,
  due: null,
  state: done
    ? { kind: 'done', by: 'user', at: '2026-11-03T00:00:00Z' }
    : { kind: 'not_started', reason: 'starting' },
  deletion_request: null,
  conversation: { kind: 'none' },
});

it('keeps a checked row pending through a read that a change notification superseded', async () => {
  const pageWith = (done: boolean) => ({ ...weekPage('2026-11-02'), items: [todo(done)] });
  listWeek.mockResolvedValueOnce(pageWith(false));
  setCheck.mockResolvedValueOnce({ kind: 'ok', result: {} });
  let answerAfterWrite: (page: TodoWeekPage) => void = () => undefined;
  let answerAfterChange: (page: TodoWeekPage) => void = () => undefined;
  listWeek.mockReturnValueOnce(new Promise((resolve) => (answerAfterWrite = resolve)));
  listWeek.mockReturnValueOnce(new Promise((resolve) => (answerAfterChange = resolve)));
  const { result } = renderHook(
    () => {
      const week = useTodoWeek({ scope: 'all' }, { project: 'any' }, null);
      const mutations = useTodoMutations({
        reloadWeek: week.reload,
        reloadDeleted: async () => undefined,
      });
      return { week, mutations };
    },
    {
      wrapper: ({ children }: { children: ReactNode }) => (
        <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
      ),
    }
  );
  await waitFor(() => expect(result.current.week.page).not.toBeNull());

  let checked: Promise<boolean> = Promise.resolve(false);
  await act(async () => {
    checked = result.current.mutations.setCheck('t1', true);
  });
  act(() => todoChanged?.());
  await waitFor(() => expect(listWeek).toHaveBeenCalledTimes(3));

  // The read after the write answers last-but-one: it is dropped, and the row still waits.
  await act(async () => answerAfterWrite(pageWith(true)));
  expect(result.current.mutations.pending.has('t1')).toBe(true);
  expect(result.current.week.page?.items[0].state.kind).toBe('not_started');

  await act(async () => answerAfterChange(pageWith(true)));
  expect(await checked).toBe(true);
  expect(result.current.mutations.pending.size).toBe(0);
  expect(result.current.week.page?.items[0].state.kind).toBe('done');
});
