import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import type {
  TodoCreateRequest,
  TodoView,
  TodoWriteResult,
} from '../../electron/src/todos/todoContracts';
import { useTodoMutations } from './useTodoMutations';

const reloadWeek = vi.fn<() => Promise<void>>();
const reloadDeleted = vi.fn<() => Promise<void>>();
const create = vi.fn<(request: TodoCreateRequest) => Promise<TodoWriteResult<TodoView>>>();
const remove = vi.fn();
const restore = vi.fn();

beforeEach(() => {
  create.mockReset();
  remove.mockReset();
  restore.mockReset();
  reloadWeek.mockReset().mockResolvedValue(undefined);
  reloadDeleted.mockReset().mockResolvedValue(undefined);
  window.electron = { todos: { create, delete: remove, restore } } as unknown as Window['electron'];
});

const wrapper = ({ children }: { children: ReactNode }) => (
  <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
);
const ok = { kind: 'ok', result: {} } as TodoWriteResult<TodoView>;

it('holds a row pending while its write is in flight, and lets it go when the write fails', async () => {
  let answer: (result: unknown) => void = () => undefined;
  remove.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
  const { result } = renderHook(() => useTodoMutations({ reloadWeek, reloadDeleted }), { wrapper });

  let deleted: Promise<boolean> = Promise.resolve(true);
  act(() => {
    deleted = result.current.deleteTodo('t1');
  });
  expect(result.current.pending.has('t1')).toBe(true);

  await act(async () => answer({ kind: 'refused', code: 'TODO_BUSY' }));
  expect(await deleted).toBe(false);
  expect(result.current.pending.size).toBe(0);
  expect(result.current.failure).toEqual({
    target: { kind: 'todo', todoId: 't1' },
    message:
      'この TODO の会話が進んでいるあいだは消せません。終わってから、または止めてから消してください。',
  });

  remove.mockRejectedValueOnce(new Error('timeout'));
  await act(() => result.current.deleteTodo('t2'));
  expect(result.current.pending.size).toBe(0);
  expect(result.current.failure?.message).toBe('うまくいきませんでした。もう一度お試しください。');
});

it('tells what the user can act on, and only a generic message for the rest', async () => {
  const { result } = renderHook(() => useTodoMutations({ reloadWeek, reloadDeleted }), { wrapper });
  restore.mockResolvedValueOnce({ kind: 'refused', code: 'TODO_RESTORE_EXPIRED' });
  await act(() => result.current.restore('t1'));
  expect(result.current.failure?.message).toBe('30 日を過ぎたので戻せません。');

  restore.mockResolvedValueOnce({ kind: 'refused', code: 'TODO_NOT_FOUND' });
  await act(() => result.current.restore('t1'));
  expect(result.current.failure?.message).toBe('うまくいきませんでした。もう一度お試しください。');

  // A write that goes through clears the last failure.
  restore.mockResolvedValueOnce(ok);
  await act(() => result.current.restore('t1'));
  expect(result.current.failure).toBeNull();
});

it('resends an unanswered add with the same id, so the same line is added once', async () => {
  const { result } = renderHook(() => useTodoMutations({ reloadWeek, reloadDeleted }), { wrapper });
  create.mockRejectedValueOnce(new Error('timeout'));
  await act(async () => {
    expect(await result.current.create('p1', '2026-11-02', '見積書を送る')).toBe(false);
  });
  expect(result.current.failure?.target).toEqual({ kind: 'add', projectId: 'p1' });

  create.mockResolvedValue(ok);
  await act(async () => {
    expect(await result.current.create('p1', '2026-11-02', '見積書を送る')).toBe(true);
  });
  await act(() => result.current.create('p1', '2026-11-02', '見積書を送る'));
  const [first, second, third] = create.mock.calls.map(([request]) => request.client_todo_id);
  expect(second).toBe(first);
  // Once answered, the next add is a new TODO.
  expect(third).not.toBe(first);
  expect(create.mock.calls[0][0]).toEqual({
    client_todo_id: first,
    project_id: 'p1',
    text: '見積書を送る',
    week_start: '2026-11-02',
    due: null,
  });
});

it('adds nothing for an empty line', async () => {
  const { result } = renderHook(() => useTodoMutations({ reloadWeek, reloadDeleted }), { wrapper });
  await act(async () => {
    expect(await result.current.create(null, '2026-11-02', '   ')).toBe(false);
  });
  expect(create).not.toHaveBeenCalled();
  expect(result.current.failure).toBeNull();
});

it('keeps a row pending until the lists showing its write have been read again', async () => {
  let weekRead: () => void = () => undefined;
  reloadWeek.mockReturnValueOnce(new Promise((resolve) => (weekRead = resolve)));
  remove.mockResolvedValueOnce(ok);
  const { result } = renderHook(() => useTodoMutations({ reloadWeek, reloadDeleted }), {
    wrapper,
  });

  let deleted: Promise<boolean> = Promise.resolve(false);
  await act(async () => {
    deleted = result.current.deleteTodo('t1');
  });
  expect(reloadWeek).toHaveBeenCalledOnce();
  expect(reloadDeleted).toHaveBeenCalledOnce();
  expect(result.current.pending.has('t1')).toBe(true);

  await act(async () => weekRead());
  expect(await deleted).toBe(true);
  expect(result.current.pending.size).toBe(0);
});
