import { useEffect, useState } from 'react';

import { todoPlaceName, type TodoPlaceName } from '@/todos/groupTodosByProject';

import type { SuggestionTodoView } from '../../../electron/src/orchestration/eventContracts';
import type { WorkspaceSettings } from '../../../electron/src/settings/workspaceSettingsFetch';

/**
 * Where the panel's TODO sits, named as the main window names it. The workspace is read again
 * for each place, so a project made since the panel opened is named too; null until it is read,
 * or when it cannot be, and then the panel shows the TODO without it.
 */
export function useTodoPlaceName(todo: SuggestionTodoView | null): TodoPlaceName | null {
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null);
  const hasTodo = todo !== null;
  const projectId = todo?.project_id ?? null;
  const organizationId = todo?.organization_id ?? null;
  useEffect(() => {
    if (!hasTodo) return;
    let current = true;
    window.electron?.workspaceSettings?.get().then(
      (read) => {
        if (current) setSettings(read);
      },
      (error: unknown) => console.error('Failed to read the organizations and projects:', error)
    );
    return () => {
      current = false;
    };
  }, [hasTodo, projectId, organizationId]);
  return todo && settings && todoPlaceName(settings, todo);
}
