import { useCallback } from 'react';

import type {
  TodoProgressPage,
  TodoProgressQuery,
  TodoScopeQuery,
} from '../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from './todoSelection';
import { useTodoRead, type TodosBridge } from './useTodoRead';

export type ProgressPeriod = TodoProgressQuery['period'];

export type TodoProgressRead = {
  page: TodoProgressPage | null;
  error: boolean;
  reload: () => Promise<void>;
};

/**
 * The progress of the TODOs in view, by `period` on the month or year holding `on` (null is
 * today's), read again when the TODOs or their progress change. Nothing in it follows a
 * conversation's state (an Action's record arrives as a TODO change), so an Action's history
 * changes do not read it again.
 */
export function useTodoProgress(
  scope: TodoScopeQuery,
  projectFilter: TodoProjectFilter,
  period: ProgressPeriod,
  on: string | null
): TodoProgressRead {
  const { scope: scopeKind, organization_id: organizationId } = scope;
  const { project, project_id: projectId } = projectFilter;
  const read = useCallback(
    (todos: TodosBridge) =>
      todos.progress({
        scope: scopeKind,
        ...(organizationId === undefined ? {} : { organization_id: organizationId }),
        project,
        ...(projectId === undefined ? {} : { project_id: projectId }),
        period,
        ...(on === null ? {} : { on }),
      }),
    [scopeKind, organizationId, project, projectId, period, on]
  );
  const { data, error, reload } = useTodoRead(read, false);
  return { page: data, error, reload };
}
