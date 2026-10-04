import { useState } from 'react';

/** What the detail column shows: one project, or the folders no project holds. */
export type WorkspaceSelection = { kind: 'project'; projectId: string } | { kind: 'unassigned' };

/**
 * Follows a change of the project set: a project that just appeared (one the user created) is
 * selected, and a selected project that went away hands the selection to the project that took
 * its place in the previous order, else the one before it.
 */
export function followProjectChange(
  previousIds: readonly string[],
  currentIds: readonly string[],
  selection: WorkspaceSelection | null
): WorkspaceSelection | null {
  const added = currentIds.find((projectId) => !previousIds.includes(projectId));
  if (added !== undefined) return { kind: 'project', projectId: added };
  if (selection?.kind !== 'project' || currentIds.includes(selection.projectId)) return selection;
  const removedIndex = previousIds.indexOf(selection.projectId);
  const remaining = previousIds.filter((projectId) => currentIds.includes(projectId));
  const successor = remaining[Math.min(removedIndex, remaining.length - 1)];
  return successor === undefined ? null : { kind: 'project', projectId: successor };
}

/** The selection to show: the chosen one while it exists, else the first project, else the
 * unassigned folders, else nothing. */
export function resolveSelection(
  projectIds: readonly string[],
  hasUnassignedFolders: boolean,
  selection: WorkspaceSelection | null
): WorkspaceSelection | null {
  if (selection?.kind === 'project' && projectIds.includes(selection.projectId)) return selection;
  if (selection?.kind === 'unassigned' && hasUnassignedFolders) return selection;
  if (projectIds.length > 0) return { kind: 'project', projectId: projectIds[0] };
  return hasUnassignedFolders ? { kind: 'unassigned' } : null;
}

/**
 * Adjusts the selection while rendering the settings that changed it, so the new detail is in
 * the same commit as the focus request that targets it (a created project's add-folder button,
 * the heading of the project that follows a deleted one). What is shown is always what is
 * stored, so a later change follows the project on screen, not an earlier choice.
 */
export function useWorkspaceSelection(
  projectIds: readonly string[] | null,
  hasUnassignedFolders: boolean
) {
  const [selection, setSelection] = useState<WorkspaceSelection | null>(null);
  const [knownIds, setKnownIds] = useState(projectIds);
  if (projectIds === null) return { selection: null, select: setSelection };

  let next = selection;
  if (!sameIds(knownIds, projectIds)) {
    setKnownIds(projectIds);
    // The first read is the baseline: nothing in it was just created.
    if (knownIds !== null) next = followProjectChange(knownIds, projectIds, selection);
  }
  const shown = resolveSelection(projectIds, hasUnassignedFolders, next);
  if (!sameSelection(shown, selection)) setSelection(shown);
  return { selection: shown, select: setSelection };
}

function sameIds(left: readonly string[] | null, right: readonly string[]): boolean {
  return left !== null && left.length === right.length && left.every((id, i) => id === right[i]);
}

function sameSelection(left: WorkspaceSelection | null, right: WorkspaceSelection | null): boolean {
  if (left === null || right === null) return left === right;
  if (left.kind === 'project')
    return right.kind === 'project' && left.projectId === right.projectId;
  return right.kind === 'unassigned';
}
