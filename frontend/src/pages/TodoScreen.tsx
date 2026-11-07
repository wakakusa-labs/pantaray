import { X } from 'lucide-react';
import { useRef, type ReactNode } from 'react';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import { DeletedTodoRow } from '@/components/todo/DeletedTodoRow';
import { TodoConversationPane } from '@/components/todo/TodoConversationPane';
import { TodoAddField, TodoProjectGroup } from '@/components/todo/TodoProjectGroup';
import { TodoRow } from '@/components/todo/TodoRow';
import { WeekSwitcher } from '@/components/todo/WeekSwitcher';
import { useI18n } from '@/context/useI18n';
import type { HistorySelection } from '@/history/historySelection';
import type { WorkspaceSettings } from '@/pages/settings/components/workspaceSettingsModel';
import { groupTodosByProject, weekTakesTodos } from '@/todos/groupTodosByProject';
import type { TodoSelection } from '@/todos/todoSelection';
import { useDeletedTodos } from '@/todos/useDeletedTodos';
import { useTodoMutations, type TodoMutationFailure } from '@/todos/useTodoMutations';
import type { TodoWeekRead } from '@/todos/useTodoWeek';

import './todoScreen.css';

const DAY_MS = 86_400_000;
const WEEK_DAYS = 7;

/** The YYYY-MM-DD `weeks` weeks from a week's first day; calendar days, so no time zone moves it. */
const shiftWeek = (start: string, weeks: number) =>
  new Date(Date.parse(`${start}T00:00:00Z`) + weeks * WEEK_DAYS * DAY_MS)
    .toISOString()
    .slice(0, 10);

/** A failed write, told under the row or the add field it was made from. */
function FailureLine({ failure }: { failure: TodoMutationFailure }) {
  return (
    <p className="todo-screen__failure" role="alert">
      {failure.message}
    </p>
  );
}

/**
 * The home page's TODO list: the week's TODOs by project (or the deleted TODOs), and beside it
 * the conversation of the TODO selected. What is shown is the page's URL; this reads and writes
 * the TODOs through the TODO hooks and draws them with the TODO parts.
 */
export function TodoScreen({
  selection,
  week,
  weekShowsSelection,
  settings,
  settingsFailed,
  onSelect,
  onOpenWork,
  onAddProject,
}: {
  selection: TodoSelection;
  /** Read by the page, whose sidebar counts the week's open TODOs too. */
  week: TodoWeekRead;
  /**
   * The page was read for the selected scope and week. Until another selection's read lands,
   * the last page stays on screen, dimmed and closed to writes: an add would go to its week.
   */
  weekShowsSelection: boolean;
  settings: WorkspaceSettings | null;
  /** The workspace could not be read, so the TODOs cannot be put in their projects. */
  settingsFailed: boolean;
  onSelect: (next: TodoSelection) => void;
  /** A work named in a conversation opens in the page's main area. */
  onOpenWork: (work: HistorySelection) => void;
  onAddProject: () => void;
}) {
  const { t } = useI18n();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const deleted = useDeletedTodos(selection.scope);
  const mutations = useTodoMutations({ reloadWeek: week.reload, reloadDeleted: deleted.reload });
  const { page } = week;
  const { failure } = mutations;
  const failureFor = (match: (target: TodoMutationFailure['target']) => boolean) =>
    failure && match(failure.target) ? <FailureLine failure={failure} /> : null;
  const projectName = (projectId: string | null) =>
    settings?.projects.find((project) => project.project_id === projectId)?.display_name ?? null;

  const showingDeleted = selection.view === 'deleted';
  const selectedTodo =
    !showingDeleted && selection.todoId !== null
      ? (page?.items.find((todo) => todo.todo_id === selection.todoId) ?? null)
      : null;
  const title = showingDeleted
    ? t('todo.nav.deleted')
    : selection.project.project === 'project'
      ? (projectName(selection.project.project_id ?? null) ?? t('todo.nav.all'))
      : selection.project.project === 'no_project'
        ? t('todo.noProject')
        : t('todo.nav.all');

  // Focus stays in the list: 「今週へ」 and the close button go away once they are used.
  const focusHeading = () => headingRef.current?.focus();
  // Weeks step from the one selected, not the one on screen, which may still be the last read.
  const showWeek = (weeks: number | null) => {
    if (!page) return;
    const start =
      weeks === null ? null : shiftWeek(selection.weekStart ?? page.current_week_start, weeks);
    onSelect({
      ...selection,
      weekStart: start === null || start === page.current_week_start ? null : start,
      todoId: null,
    });
  };
  /** A read that failed after one landed: what is shown may be out of date. */
  const staleAlert = (
    <p className="history-error" role="alert">
      {t('todo.page.loadFailedStale')}
    </p>
  );

  const renderWeek = (): ReactNode => {
    if (!page || !settings) {
      if ((week.error && !page) || settingsFailed)
        return (
          <p className="history-error" role="alert">
            {t('todo.page.loadFailed')}
          </p>
        );
      return (
        <p className="history-loading" role="status">
          {t('todo.page.loading')}
        </p>
      );
    }
    const takesTodos = weekTakesTodos(page);
    const groups = groupTodosByProject(page, settings, selection.scope, selection.project);
    return (
      <>
        <WeekSwitcher
          week={page.week}
          isCurrent={page.week.start === page.current_week_start}
          onPrevious={() => showWeek(-1)}
          onNext={() => showWeek(1)}
          onCurrent={() => {
            showWeek(null);
            focusHeading();
          }}
        />
        {/* What was read before the failure stays; the next change reads again. */}
        {week.error ? staleAlert : null}
        {/* A disabled fieldset closes every control in it to the keyboard and the pointer. */}
        <fieldset
          className={
            weekShowsSelection ? 'todo-screen__week' : 'todo-screen__week todo-screen__week--stale'
          }
          disabled={!weekShowsSelection}
          aria-busy={!weekShowsSelection && !week.error}
        >
          {groups.length === 0 ? (
            <p className="todo-screen__empty">
              {takesTodos ? t('todo.page.noProjects') : t('todo.page.emptyWeek')}
            </p>
          ) : null}
          {groups.map((group) => (
            <TodoProjectGroup
              key={group.projectId ?? ''}
              projectName={group.projectName}
              organizationName={
                group.organization === null
                  ? null
                  : group.organization.kind === 'organization'
                    ? group.organization.name
                    : t('todo.rail.none')
              }
              doneCount={group.doneCount}
              totalCount={group.totalCount}
              addField={
                takesTodos ? (
                  <>
                    <TodoAddField
                      onSubmit={(text) => mutations.create(group.projectId, page.week.start, text)}
                    />
                    {failureFor(
                      (target) => target.kind === 'add' && target.projectId === group.projectId
                    )}
                  </>
                ) : null
              }
            >
              {group.items.flatMap((todo) => {
                const request = todo.deletion_request;
                const rowFailure = failureFor(
                  (target) => target.kind === 'todo' && target.todoId === todo.todo_id
                );
                return [
                  <TodoRow
                    key={todo.todo_id}
                    todo={todo}
                    today={page.today}
                    selected={todo.todo_id === selectedTodo?.todo_id}
                    pending={mutations.pending.has(todo.todo_id)}
                    onToggleCheck={() =>
                      void mutations.setCheck(todo.todo_id, todo.state.kind !== 'done')
                    }
                    onSelect={() => onSelect({ ...selection, todoId: todo.todo_id })}
                    onDelete={() => void mutations.deleteTodo(todo.todo_id)}
                    onAnswerDeletion={(answer) => {
                      if (request)
                        void mutations.answerDeletion(todo.todo_id, request.request_id, answer);
                    }}
                  />,
                  rowFailure ? (
                    <li key={`${todo.todo_id}:failure`} className="todo-screen__failure-row">
                      {rowFailure}
                    </li>
                  ) : null,
                ];
              })}
            </TodoProjectGroup>
          ))}
        </fieldset>
      </>
    );
  };

  const renderDeleted = (): ReactNode => {
    const { items } = deleted;
    if (!items || !page) {
      if (deleted.error || (week.error && !page))
        return (
          <p className="history-error" role="alert">
            {t('todo.page.loadFailed')}
          </p>
        );
      return (
        <p className="history-loading" role="status">
          {t('todo.page.loading')}
        </p>
      );
    }
    // What was read before the failure stays; the next change reads again.
    const alert = deleted.error ? staleAlert : null;
    if (items.length === 0)
      return (
        <>
          {alert}
          <p className="todo-screen__empty">{t('todo.deleted.empty')}</p>
        </>
      );
    return (
      <>
        {alert}
        <ul className="todo-screen__deleted">
          {items.flatMap((item) => {
            const rowFailure = failureFor(
              (target) => target.kind === 'todo' && target.todoId === item.todo_id
            );
            return [
              <DeletedTodoRow
                key={item.todo_id}
                item={item}
                projectName={projectName(item.project_id)}
                today={page.today}
                pending={mutations.pending.has(item.todo_id)}
                onRestore={() => void mutations.restore(item.todo_id)}
              />,
              rowFailure ? (
                <li key={`${item.todo_id}:failure`} className="todo-screen__failure-row">
                  {rowFailure}
                </li>
              ) : null,
            ];
          })}
        </ul>
      </>
    );
  };

  return (
    <div className={selectedTodo ? 'todo-screen todo-screen--with-pane' : 'todo-screen'}>
      <section className="todo-screen__list" aria-labelledby="todo-screen-title">
        <header className="history-pane-header todo-screen__header">
          <h1 id="todo-screen-title" ref={headingRef} tabIndex={-1}>
            {title}
          </h1>
          {selectedTodo ? (
            <button
              type="button"
              className="action-task__icon-button"
              aria-label={t('todo.conversation.close')}
              data-tooltip={t('todo.conversation.close')}
              onClick={() => {
                onSelect({ ...selection, todoId: null });
                focusHeading();
              }}
            >
              <X size={17} strokeWidth={1.8} aria-hidden="true" />
            </button>
          ) : null}
        </header>
        <div className="todo-screen__scroll">
          <AiConnectionNotice />
          {showingDeleted ? renderDeleted() : renderWeek()}
        </div>
      </section>
      {selectedTodo ? (
        <div className="history-detail todo-screen__conversation">
          <OpenWorkContext.Provider value={onOpenWork}>
            <TodoConversationPane
              key={selectedTodo.todo_id}
              todo={selectedTodo}
              onAddProject={onAddProject}
            />
          </OpenWorkContext.Provider>
        </div>
      ) : null}
    </div>
  );
}
