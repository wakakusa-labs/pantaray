import type { TodoImportDraft, TodoImportView } from '../../electron/src/todos/todoImportContracts';

export type DraftGroupView = TodoImportView['groups'][number];
export type DraftTodoView = DraftGroupView['todos'][number];
/** What the user changes on one TODO of the draft. */
export type DraftTodoPatch = Partial<
  Pick<DraftTodoView, 'text' | 'due' | 'starts_on' | 'keep_duplicate'>
>;
/** A TODO's place in the draft: its group's index and its own within the group. */
export type DraftTodoAt = Readonly<{ group: number; todo: number }>;

/**
 * The draft a view shows, as a write takes it whole: each group by its names (whether a name is
 * new is the backend's to decide again), each TODO without its duplicate mark.
 */
export function draftOf(groups: readonly DraftGroupView[]): TodoImportDraft {
  return {
    groups: groups.map((group) => ({
      organization: group.organization?.name ?? null,
      project: group.project?.name ?? null,
      todos: group.todos.map(({ text, due, starts_on, keep_duplicate }) => ({
        text,
        due,
        starts_on,
        keep_duplicate,
      })),
    })),
  };
}

/** The draft with one TODO changed. */
export function editDraftTodo(
  groups: readonly DraftGroupView[],
  at: DraftTodoAt,
  patch: DraftTodoPatch
): TodoImportDraft {
  return draftOf(
    groups.map((group, groupIndex) =>
      groupIndex !== at.group
        ? group
        : {
            ...group,
            todos: group.todos.map((todo, todoIndex) =>
              todoIndex === at.todo ? { ...todo, ...patch } : todo
            ),
          }
    )
  );
}

/** The draft without one TODO; a group left with none goes too, so it creates nothing. */
export function removeDraftTodo(
  groups: readonly DraftGroupView[],
  at: DraftTodoAt
): TodoImportDraft {
  return draftOf(
    groups.flatMap((group, groupIndex) => {
      if (groupIndex !== at.group) return [group];
      const todos = group.todos.filter((_, todoIndex) => todoIndex !== at.todo);
      return todos.length === 0 ? [] : [{ ...group, todos }];
    })
  );
}

/** How many TODOs registering creates: a duplicate counts only when the user keeps it. */
export function registeredCount(groups: readonly DraftGroupView[]): number {
  return groups.reduce(
    (sum, group) =>
      sum + group.todos.filter((todo) => !todo.duplicate || todo.keep_duplicate).length,
    0
  );
}
