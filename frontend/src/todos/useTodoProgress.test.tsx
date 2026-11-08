import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type { OrchestrationStatus } from '../../electron/src/orchestration/eventContracts';
import type { TodoProgressPage, TodoProgressQuery } from '../../electron/src/todos/todoContracts';
import { useTodoProgress, type ProgressPeriod } from './useTodoProgress';
import { useTodoProgressMutations } from './useTodoProgressMutations';

let todoChanged: (() => void) | null;
let statusChanged: ((status: OrchestrationStatus) => void) | null;
const progress = vi.fn<(query: TodoProgressQuery) => Promise<TodoProgressPage>>();
const setGoal = vi.fn();
const clearGoal = vi.fn();
const deleteProgressRecord = vi.fn();

const progressPage = (today: string): TodoProgressPage => ({
  today,
  period: 'day',
  page_start: '2026-11-01',
  rows: [],
  goals: [],
  records: [],
});

beforeEach(() => {
  todoChanged = null;
  statusChanged = null;
  progress.mockReset();
  setGoal.mockReset();
  clearGoal.mockReset();
  deleteProgressRecord.mockReset();
  window.electron = {
    todos: {
      progress,
      setGoal,
      clearGoal,
      deleteProgressRecord,
      onChanged: (callback: () => void) => {
        todoChanged = callback;
        return () => (todoChanged = null);
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

const wrapper = ({ children }: { children: ReactNode }) => (
  <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
);

it('reads the progress asked for, and again when the TODOs change or a WS session starts', async () => {
  progress.mockResolvedValue(progressPage('2026-11-07'));
  const { result, rerender } = renderHook(
    ({ period, on }: { period: ProgressPeriod; on: string | null }) =>
      useTodoProgress(
        { scope: 'organization', organization_id: 'org-1' },
        { project: 'project', project_id: 'p1' },
        period,
        on
      ),
    { initialProps: { period: 'day', on: null } }
  );
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-07'));
  expect(progress).toHaveBeenCalledWith({
    scope: 'organization',
    organization_id: 'org-1',
    project: 'project',
    project_id: 'p1',
    period: 'day',
  });

  rerender({ period: 'month', on: '2025-03-01' });
  await waitFor(() =>
    expect(progress).toHaveBeenLastCalledWith(
      expect.objectContaining({ period: 'month', on: '2025-03-01' })
    )
  );

  progress.mockResolvedValue(progressPage('2026-11-08'));
  act(() => todoChanged?.());
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-08'));

  progress.mockResolvedValue(progressPage('2026-11-09'));
  act(() => statusChanged?.({ status: 'session_started' }));
  await waitFor(() => expect(result.current.page?.today).toBe('2026-11-09'));
});

it('sends the goal for its project and tells a deadline before the set day', async () => {
  const reloadProgress = vi.fn(async () => undefined);
  const { result } = renderHook(() => useTodoProgressMutations({ reloadProgress }), { wrapper });
  const goal = { text: '売上', target: 300_000_000, unit: '円', deadline: '2026-09-01' };

  setGoal.mockResolvedValueOnce({ kind: 'refused', code: 'TODO_GOAL_DEADLINE_BEFORE_SET' });
  await act(async () => {
    expect(await result.current.setGoal('p1', goal)).toBe(false);
  });
  expect(setGoal).toHaveBeenCalledWith({ project_id: 'p1', ...goal });
  expect(result.current.failure).toEqual({
    target: { kind: 'goal', projectId: 'p1' },
    message: '期限は、目標を決めた日か、それより後の日にしてください。',
  });
  expect(reloadProgress).not.toHaveBeenCalled();

  clearGoal.mockResolvedValueOnce({ kind: 'ok', result: null });
  await act(async () => {
    expect(await result.current.clearGoal('p1')).toBe(true);
  });
  expect(result.current.failure).toBeNull();
  expect(reloadProgress).toHaveBeenCalledOnce();
});

it('keeps a record pending until its deletion has been read again, and tells a failed one', async () => {
  let progressRead: () => void = () => undefined;
  const reloadProgress = vi.fn(() => new Promise<void>((resolve) => (progressRead = resolve)));
  const { result } = renderHook(() => useTodoProgressMutations({ reloadProgress }), { wrapper });

  deleteProgressRecord.mockResolvedValueOnce(undefined);
  let deleted: Promise<boolean> = Promise.resolve(false);
  await act(async () => {
    deleted = result.current.deleteRecord('r1');
  });
  expect(deleteProgressRecord).toHaveBeenCalledWith({ record_id: 'r1' });
  expect(result.current.pendingRecordIds.has('r1')).toBe(true);
  await act(async () => progressRead());
  expect(await deleted).toBe(true);
  expect(result.current.pendingRecordIds.size).toBe(0);

  deleteProgressRecord.mockRejectedValueOnce(new Error('timeout'));
  await act(() => result.current.deleteRecord('r2'));
  expect(result.current.pendingRecordIds.size).toBe(0);
  expect(result.current.failure).toEqual({
    target: { kind: 'record', recordId: 'r2' },
    message: 'うまくいきませんでした。もう一度お試しください。',
  });
});
