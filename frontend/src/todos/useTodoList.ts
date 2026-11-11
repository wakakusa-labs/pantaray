import { useCallback } from 'react';

import type {
  TodoListPage,
  TodoListQuery,
  TodoScopeQuery,
} from '../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from './todoSelection';
import { useTodoRead, type TodosBridge } from './useTodoRead';

export type TodoListSort = NonNullable<TodoListQuery['sort']>;

export type TodoListRead = {
  page: TodoListPage | null;
  error: boolean;
  reload: () => Promise<void>;
};

/**
 * Every TODO of the scope and project, open ones first in `sort` order, read again when the TODOs
 * change and when an Action starts, waits or ends, since a row's state follows its conversation.
 */
export function useTodoList(
  scope: TodoScopeQuery,
  projectFilter: TodoProjectFilter,
  sort: TodoListSort
): TodoListRead {
  const { scope: scopeKind, organization_id: organizationId } = scope;
  const { project, project_id: projectId } = projectFilter;
  const read = useCallback(
    (todos: TodosBridge) =>
      todos.list({
        scope: scopeKind,
        ...(organizationId === undefined ? {} : { organization_id: organizationId }),
        project,
        ...(projectId === undefined ? {} : { project_id: projectId }),
        sort,
      }),
    [scopeKind, organizationId, project, projectId, sort]
  );
  const { data, error, reload } = useTodoRead(read, true);
  return { page: data, error, reload };
}
