import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import { useRef, type ReactNode } from 'react';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import type { WorkspaceProjects } from '@/components/history/HistoryProjects';
import { ProjectFolders } from '@/components/history/ProjectFolders';
import { DeletedTodoRow } from '@/components/todo/DeletedTodoRow';
import { ProgressScreen } from '@/components/todo/progress/ProgressScreen';
import { TodoAddBar } from '@/components/todo/TodoAddBar';
import { TodoConversationPane } from '@/components/todo/TodoConversationPane';
import { TodoProjectGroup } from '@/components/todo/TodoProjectGroup';
import { TodoRow } from '@/components/todo/TodoRow';
import { weekEnds } from '@/components/todo/todoDates';
import { useI18n } from '@/context/useI18n';
import type { HistorySelection } from '@/history/historySelection';
import type { WorkspaceSettings } from '@/pages/settings/components/workspaceSettingsModel';
import { workspacePendingKey } from '@/pages/settings/useWorkspaceSettingsController';
import { groupTodosByProject, todoProjectSlots, weekTakesTodos } from '@/todos/groupTodosByProject';
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

/** A failed write, told under the row or the add row it was made from. */
function FailureLine({ failure }: { failure: TodoMutationFailure }) {
  return (
    <p className="todo-screen__failure" role="alert">
      {failure.message}
    </p>
  );
}

/** What the header's folders of the project in view are changed with. */
type ProjectFolderActions = Pick<
  WorkspaceProjects,
  'pending' | 'addFolderToProject' | 'removeFolderFromProject' | 'openFolder'
>;

/**
 * The home page's TODO list: the week's TODOs by project (or their progress, or the deleted
 * TODOs), with the folders of the one project in view under its header, and beside the week the
 * conversation of the TODO selected. What is shown is the page's URL; this reads and writes
 * the TODOs through the TODO hooks and draws them with the TODO parts.
 */
export function TodoScreen({
  selection,
  week,
  weekShowsSelection,
  settings,
  settingsFailed,
  folderActions,
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
  folderActions: ProjectFolderActions;
  onSelect: (next: TodoSelection) => void;
  /** A work named in a conversation opens in the page's main area. */
  onOpenWork: (work: HistorySelection) => void;
  onAddProject: () => void;
}) {
  const { t, language } = useI18n();
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
    selection.view === 'list' && selection.todoId !== null
      ? (page?.items.find((todo) => todo.todo_id === selection.todoId) ?? null)
      : null;
  const showingWeek = selection.view === 'list';
  const selectedProjectId =
    selection.project.project === 'project' ? (selection.project.project_id ?? null) : null;
  const isCurrentWeek = !page || page.week.start === page.current_week_start;
  const weekRange = page
    ? t('todo.week.range', weekEnds(language, page.week.start, page.week.end))
    : null;
  // The week is named by its days away from this week; the groups name its projects.
  const title = showingWeek
    ? isCurrentWeek
      ? t('todo.week.title')
      : weekRange
    : t('todo.nav.deleted');

  // Focus returns to the list: 「今週へ」 and the conversation's close button go away once used.
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
  const loadFailed = (
    <p className="history-error" role="alert">
      {t('todo.page.loadFailed')}
    </p>
  );
  const loading = (
    <p className="history-loading" role="status">
      {t('todo.page.loading')}
    </p>
  );

  const projects = settings ? todoProjectSlots(settings, selection.scope, selection.project) : [];
  const groups = page ? groupTodosByProject(page, projects) : [];

  const renderWeek = (): ReactNode => {
    if (!page || !settings) return (week.error && !page) || settingsFailed ? loadFailed : loading;
    return (
      <>
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
              {projects.length === 0 ? t('todo.page.noProjects') : t('todo.page.emptyWeek')}
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
            >
              {group.items.flatMap((todo) => {
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
                    onSetDue={(due) => void mutations.setDue(todo.todo_id, due)}
                    onSetStart={(startsOn) => void mutations.setStart(todo.todo_id, startsOn)}
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

  /** The week's steps and how much of what is in view is done, beside the week's name. */
  const renderWeekControls = (): ReactNode => {
    if (!page) return null;
    const done = groups.reduce((sum, group) => sum + group.doneCount, 0);
    const total = groups.reduce((sum, group) => sum + group.totalCount, 0);
    return (
      <>
        {isCurrentWeek ? <span className="todo-screen__range">{weekRange}</span> : null}
        <span className="todo-screen__steps">
          <button
            type="button"
            className="todo-screen__step"
            aria-label={t('todo.week.previous')}
            data-tooltip={t('todo.week.previous')}
            onClick={() => showWeek(-1)}
          >
            <ChevronLeft size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="todo-screen__step"
            aria-label={t('todo.week.next')}
            data-tooltip={t('todo.week.next')}
            onClick={() => showWeek(1)}
          >
            <ChevronRight size={15} aria-hidden="true" />
          </button>
          {isCurrentWeek ? null : (
            <button
              type="button"
              className="history-filter-button todo-screen__current"
              onClick={() => {
                showWeek(null);
                focusHeading();
              }}
            >
              {t('todo.week.current')}
            </button>
          )}
        </span>
        {total > 0 ? (
          <span className="todo-screen__summary">
            {t('todo.week.doneCount', { done, total })}
            <span className="todo-screen__track" aria-hidden="true">
              <span style={{ width: `${(done / total) * 100}%` }} />
            </span>
          </span>
        ) : null}
      </>
    );
  };

  /** The bar under the week that adds a TODO, in a week that takes them. */
  const renderAddBar = (): ReactNode => {
    if (!page || !settings || !weekTakesTodos(page) || projects.length === 0) return null;
    return (
      <div className="todo-screen__add">
        {failureFor((target) => target.kind === 'add')}
        <TodoAddBar
          today={page.today}
          projects={projects}
          disabled={!weekShowsSelection}
          onSubmit={mutations.create}
        />
      </div>
    );
  };

  const renderDeleted = (): ReactNode => {
    const { items } = deleted;
    if (!items || !page) return deleted.error || (week.error && !page) ? loadFailed : loading;
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

  // The progress is a page of its own under the same layout, with its own header and pane. The
  // projects' names come with the workspace, so it waits for it as the week does.
  if (selection.view === 'progress')
    return (
      <ProgressScreen
        selection={selection}
        // A project created since the workspace was read is named once it is read again.
        projectName={settings ? (projectId) => projectName(projectId) ?? t('todo.noProject') : null}
        settingsFailed={settingsFailed}
        onSelect={onSelect}
        onOpenEvidence={(actionId) => onOpenWork(`action:${actionId}`)}
      />
    );

  return (
    <div className={selectedTodo ? 'todo-screen todo-screen--with-pane' : 'todo-screen'}>
      <section className="todo-screen__list" aria-labelledby="todo-screen-title">
        <header className="history-pane-header todo-screen__header">
          <h1 id="todo-screen-title" ref={headingRef} tabIndex={-1}>
            {title}
          </h1>
          {showingWeek ? renderWeekControls() : null}
        </header>
        {showingWeek && settings && selectedProjectId !== null ? (
          <div className="todo-screen__folders">
            <span className="todo-screen__folders-label" aria-hidden="true">
              {t('history.projects.folders')}
            </span>
            <ProjectFolders
              key={selectedProjectId}
              projectId={selectedProjectId}
              folders={settings.folders.filter((folder) =>
                folder.project_ids.includes(selectedProjectId)
              )}
              isFolderPending={(folderId) =>
                folderActions.pending.has(workspacePendingKey.folder(folderId))
              }
              onOpenFolder={(folderId) => void folderActions.openFolder(folderId)}
              onRemoveFolder={(folderId) =>
                folderActions.removeFolderFromProject(folderId, selectedProjectId)
              }
              onAddFolder={() => void folderActions.addFolderToProject(selectedProjectId)}
              t={t}
            />
          </div>
        ) : null}
        <div className="todo-screen__scroll">
          <AiConnectionNotice />
          {showingDeleted ? renderDeleted() : renderWeek()}
        </div>
        {showingWeek ? renderAddBar() : null}
      </section>
      {selectedTodo ? (
        <div className="history-detail todo-screen__conversation">
          <OpenWorkContext.Provider value={onOpenWork}>
            <TodoConversationPane
              key={selectedTodo.todo_id}
              todo={selectedTodo}
              pending={mutations.pending.has(selectedTodo.todo_id)}
              deletionFailure={failureFor(
                (target) => target.kind === 'deletion' && target.todoId === selectedTodo.todo_id
              )}
              onAnswerDeletion={(requestId, answer) =>
                void mutations.answerDeletion(selectedTodo.todo_id, requestId, answer)
              }
              onAddProject={onAddProject}
              closeButton={
                <button
                  type="button"
                  className="action-task__icon-button action-task__close"
                  aria-label={t('todo.conversation.close')}
                  data-tooltip={t('todo.conversation.close')}
                  onClick={() => {
                    onSelect({ ...selection, todoId: null });
                    focusHeading();
                  }}
                >
                  <X size={17} strokeWidth={1.8} aria-hidden="true" />
                </button>
              }
            />
          </OpenWorkContext.Provider>
        </div>
      ) : null}
    </div>
  );
}
