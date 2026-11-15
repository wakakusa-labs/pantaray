import { act, renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import type {
  OpenTodoImport,
  TodoImportResult,
  TodoImportStartRequest,
  TodoImportView,
} from '../../electron/src/todos/todoImportContracts';
import { useOpenTodoImports, useTodoImportStart } from './useTodoImport';

const start = vi.fn<(request: TodoImportStartRequest) => Promise<TodoImportResult>>();

const opened = (importId: string): TodoImportResult => ({
  kind: 'ok',
  result: {
    import_id: importId,
    scope: 'all',
    organization_id: null,
    revision: 1,
    state: 'open',
    action_id: 'action-1',
    groups: [],
    issue: null,
    result: null,
  } satisfies TodoImportView,
});

beforeEach(() => {
  start.mockReset().mockImplementation(async (request) => opened(request.import_id));
  window.electron = { todoImports: { start } } as unknown as Window['electron'];
});

const NONE = { files: [], images: [] };
const IMAGE = { kind: 'image' as const, storage_path: 'images/board.png' };
const FILE = {
  attachment_id: '6f1c7a64-5d1e-4c4b-9b5b-3d0d7c3f2a10',
  name: 'notes.md',
  byte_size: 12,
};

it('starts in the scope in view, naming the organization only for an organization’s', async () => {
  const { result } = renderHook(() => useTodoImportStart());
  await act(() =>
    result.current.start({ scope: 'organization', organization_id: 'xer' }, 'a\nb', NONE)
  );
  expect(start).toHaveBeenLastCalledWith({
    import_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    scope: 'organization',
    organization_id: 'xer',
    text: 'a\nb',
  });

  let importId: string | null = null;
  await act(async () => {
    importId = await result.current.start({ scope: 'no_organization' }, '  ', {
      files: [FILE],
      images: [],
    });
  });
  expect(start).toHaveBeenLastCalledWith({
    import_id: importId,
    scope: 'no_organization',
    files: [FILE],
  });
  await act(() => result.current.start({ scope: 'all' }, 'a\nb', NONE));
  expect(start.mock.lastCall?.[0]).not.toHaveProperty('organization_id');

  // Images alone start one, and go with documents.
  await act(() => result.current.start({ scope: 'all' }, '', { files: [], images: [IMAGE] }));
  expect(start.mock.lastCall?.[0]).toEqual({
    import_id: expect.any(String),
    scope: 'all',
    images: [IMAGE],
  });
  await act(() => result.current.start({ scope: 'all' }, '', { files: [FILE], images: [IMAGE] }));
  expect(start.mock.lastCall?.[0]).toEqual({
    import_id: expect.any(String),
    scope: 'all',
    files: [FILE],
    images: [IMAGE],
  });
});

it('resends an unanswered start under the same id, and a changed one under a new id', async () => {
  const { result } = renderHook(() => useTodoImportStart());
  start.mockRejectedValueOnce(new Error('timeout'));
  await act(() => result.current.start({ scope: 'all' }, 'a\nb', { files: [FILE], images: [] }));
  expect(result.current.problem).toBe('failed');
  const firstId = start.mock.lastCall?.[0].import_id;

  await act(() => result.current.start({ scope: 'all' }, 'a\nb', { files: [FILE], images: [] }));
  expect(start.mock.lastCall?.[0].import_id).toBe(firstId);
  expect(result.current.problem).toBeNull();

  // Answered, so the next start is a new import.
  await act(() => result.current.start({ scope: 'all' }, 'a\nb', { files: [FILE], images: [] }));
  expect(start.mock.lastCall?.[0].import_id).not.toBe(firstId);

  start.mockResolvedValueOnce({
    kind: 'refused',
    refusal: { reason: 'files_unavailable', group_index: null, todo_index: null },
  });
  let refused: string | null = 'x';
  await act(async () => {
    refused = await result.current.start({ scope: 'all' }, '', { files: [FILE], images: [] });
  });
  expect(refused).toBeNull();
  expect(result.current.problem).toBe('files_unavailable');
});

it('shows no other scope’s open imports while the new scope’s are read or fail', async () => {
  const listOpen = vi.fn(
    async (query: TodoScopeQuery): Promise<OpenTodoImport[]> =>
      query.organization_id === 'xer'
        ? [{ import_id: 'I1', request: 'a', started_at: '2026-11-04T03:00:00.000Z' }]
        : Promise.reject(new Error('backend down'))
  );
  window.electron = {
    todoImports: { listOpen },
    todos: { onChanged: () => () => undefined },
  } as unknown as Window['electron'];
  const { result, rerender } = renderHook(
    ({ scope }: { scope: TodoScopeQuery }) => useOpenTodoImports(scope),
    { initialProps: { scope: { scope: 'organization', organization_id: 'xer' } } }
  );
  await act(() => result.current.reload());
  expect(result.current.items.map((item) => item.import_id)).toEqual(['I1']);

  rerender({ scope: { scope: 'organization', organization_id: 'wakakusa' } });
  expect(result.current.items).toEqual([]);
  await act(() => result.current.reload());
  expect(result.current.items).toEqual([]);
});
