import { useCallback } from 'react';

import type { DeletedTodoView, TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import { useTodoRead, type TodosBridge } from './useTodoRead';

export type DeletedTodosRead = {
  items: DeletedTodoView[] | null;
  error: boolean;
  reload: () => Promise<void>;
};

/** The scope's deleted TODOs, newest first, read again when the TODOs change. */
export function useDeletedTodos(scope: TodoScopeQuery): DeletedTodosRead {
  const { scope: scopeKind, organization_id: organizationId } = scope;
  const read = useCallback(
    (todos: TodosBridge) =>
      todos.listDeleted({
        scope: scopeKind,
        ...(organizationId === undefined ? {} : { organization_id: organizationId }),
      }),
    [scopeKind, organizationId]
  );
  const { data, error, reload } = useTodoRead(read, false);
  return { items: data, error, reload };
}
