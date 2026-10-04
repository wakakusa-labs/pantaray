import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  followProjectChange,
  resolveSelection,
  useWorkspaceSelection,
} from './useWorkspaceSelection';

const project = (projectId: string) => ({ kind: 'project' as const, projectId });

describe('workspace selection', () => {
  it('selects a project that just appeared', () => {
    expect(followProjectChange(['a', 'b'], ['a', 'b', 'c'], project('a'))).toEqual(project('c'));
  });

  it('hands a deleted selection to the next project, else the one before it', () => {
    expect(followProjectChange(['a', 'b', 'c'], ['a', 'c'], project('b'))).toEqual(project('c'));
    expect(followProjectChange(['a', 'b', 'c'], ['a', 'b'], project('c'))).toEqual(project('b'));
    expect(followProjectChange(['a'], [], project('a'))).toBeNull();
    // A reorder or another project's deletion keeps the selection.
    expect(followProjectChange(['a', 'b', 'c'], ['c', 'a'], project('a'))).toEqual(project('a'));
  });

  it('falls back to the first project, then the unassigned folders, then nothing', () => {
    expect(resolveSelection(['a', 'b'], true, project('gone'))).toEqual(project('a'));
    expect(resolveSelection(['a'], false, { kind: 'unassigned' })).toEqual(project('a'));
    expect(resolveSelection([], true, null)).toEqual({ kind: 'unassigned' });
    expect(resolveSelection([], false, null)).toBeNull();
  });

  it('keeps the shown project selected when the projects are reordered', () => {
    const { result, rerender } = renderHook(
      ({ ids }: { ids: string[] | null }) => useWorkspaceSelection(ids, false),
      { initialProps: { ids: null as string[] | null } }
    );
    rerender({ ids: ['a', 'b'] });
    expect(result.current.selection).toEqual(project('a'));

    rerender({ ids: ['b', 'a'] });
    expect(result.current.selection).toEqual(project('a'));
  });

  it('follows the shown project after the unassigned folders fall back to it', () => {
    const { result, rerender } = renderHook(
      ({ ids, unassigned }: { ids: string[]; unassigned: boolean }) =>
        useWorkspaceSelection(ids, unassigned),
      { initialProps: { ids: ['a', 'b'], unassigned: true } }
    );
    act(() => result.current.select({ kind: 'unassigned' }));
    rerender({ ids: ['a', 'b'], unassigned: false });
    expect(result.current.selection).toEqual(project('a'));

    // Deleting the shown project leaves a folder unassigned again; the next project takes over.
    rerender({ ids: ['b'], unassigned: true });
    expect(result.current.selection).toEqual(project('b'));
  });
});
