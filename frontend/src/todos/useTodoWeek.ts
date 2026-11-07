import { useCallback } from 'react';

import type { TodoScopeQuery, TodoWeekPage } from '../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from './todoSelection';
import { useTodoRead, type TodosBridge } from './useTodoRead';

export type TodoWeekRead = {
  page: TodoWeekPage | null;
  error: boolean;
  reload: () => Promise<void>;
};

/**
 * One week's TODOs, read again when the TODOs change and when an Action starts, waits or ends,
 * since a row's state follows its conversation. Writes are not anticipated here: a row waits for
 * its write (`useTodoMutations`) and then for this read.
 */
export function useTodoWeek(
  scope: TodoScopeQuery,
  projectFilter: TodoProjectFilter,
  weekStart: string | null
): TodoWeekRead {
  const { scope: scopeKind, organization_id: organizationId } = scope;
  const { project, project_id: projectId } = projectFilter;
  const read = useCallback(
    (todos: TodosBridge) =>
      todos.listWeek({
        scope: scopeKind,
        ...(organizationId === undefined ? {} : { organization_id: organizationId }),
        project,
        ...(projectId === undefined ? {} : { project_id: projectId }),
        ...(weekStart === null ? {} : { week_start: weekStart }),
      }),
    [scopeKind, organizationId, project, projectId, weekStart]
  );
  const { data, error, reload } = useTodoRead(read, true);
  return { page: data, error, reload };
}
