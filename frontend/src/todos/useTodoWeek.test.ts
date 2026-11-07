import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { TodoWeekPage, TodoWeekQuery } from '../../electron/src/todos/todoContracts';
import { useDeletedTodos } from './useDeletedTodos';
import { useTodoWeek } from './useTodoWeek';

let todoChanged: (() => void) | null;
let historyChanged: (() => void) | null;
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
  listWeek.mockReset();
  listDeleted.mockReset();
  window.electron = {
    todos: {
      listWeek,
      listDeleted,
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
