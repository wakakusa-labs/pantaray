import { useEffect, useState } from 'react';

import { todoPlaceName, type TodoPlaceName } from '@/todos/groupTodosByProject';

import type { SuggestionTodoView } from '../../../electron/src/orchestration/eventContracts';
import type { WorkspaceSettings } from '../../../electron/src/settings/workspaceSettingsFetch';

/**
 * Where the panel's TODO sits, named as the main window names it. The panel is reused and no
 * event says the workspace changed, so it is read again for each TODO the panel is given: a
 * project made, renamed or moved since is named as it is now. Null until it is read, or when it
 * cannot be, and then the panel shows the TODO without it.
 */
export function useTodoPlaceName(todo: SuggestionTodoView | null): TodoPlaceName | null {
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null);
  useEffect(() => {
    if (todo === null) return;
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
  }, [todo]);
  return todo && settings && todoPlaceName(settings, todo);
}
