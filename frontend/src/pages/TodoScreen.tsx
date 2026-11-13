import { X } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import type { WorkspaceProjects } from '@/components/history/HistoryProjects';
import { ProjectFolders } from '@/components/history/ProjectFolders';
import { DeletedTodoRow } from '@/components/todo/DeletedTodoRow';
import { OpenTodoImportRows } from '@/components/todo/OpenTodoImportRows';
import { ProgressScreen } from '@/components/todo/progress/ProgressScreen';
import { TodoAddBar } from '@/components/todo/TodoAddBar';
import { TodoConversationPane } from '@/components/todo/TodoConversationPane';
import { TodoImportPane } from '@/components/todo/TodoImportPane';
import { TodoProjectGroup } from '@/components/todo/TodoProjectGroup';
import { TodoRow } from '@/components/todo/TodoRow';
import { useTodoPaneWidth } from '@/components/todo/useTodoPaneWidth';
import { useI18n } from '@/context/useI18n';
import type { HistorySelection } from '@/history/historySelection';
import type { WorkspaceSettings } from '@/pages/settings/components/workspaceSettingsModel';
import { workspacePendingKey } from '@/pages/settings/useWorkspaceSettingsController';
import { groupTodosByProject, todoProjectSlots } from '@/todos/groupTodosByProject';
import type { TodoListSort, TodoSelection } from '@/todos/todoSelection';
import { useDeletedTodos } from '@/todos/useDeletedTodos';
import {
  useOpenTodoImports,
  useTodoImportStart,
  type TodoImportAttachments,
  type TodoImportProblem,
} from '@/todos/useTodoImport';
import type { TodoListRead } from '@/todos/useTodoList';
import { useTodoMutations, type TodoMutationFailure } from '@/todos/useTodoMutations';

import type { TodoView } from '../../electron/src/todos/todoContracts';
import './todoScreen.css';

const SORTS: readonly TodoListSort[] = ['added', 'due'];

/** A failed write, told under the row or the add row it was made from. */
function FailureLine({ failure }: { failure: TodoMutationFailure }) {
  return (
    <p className="todo-screen__failure" role="alert">
      {failure.message}
    </p>
  );
}

/** The pane's close, last in its header. */
function CloseButton({ label, onClose }: { label: string; onClose: () => void }) {
  return (
    <button
      type="button"
      className="action-task__icon-button action-task__close"
      aria-label={label}
      data-tooltip={label}
      onClick={onClose}
    >
      <X size={17} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}

/** Why an import did not start, told under the add bar. */
const startProblemMessage = (problem: TodoImportProblem) =>
  problem === 'files_unavailable' || problem === 'images_invalid'
    ? 'todo.import.filesUnavailable'
    : 'todo.import.startFailed';

/** What the header's folders of the project in view are changed with. */
type ProjectFolderActions = Pick<
  WorkspaceProjects,
  'pending' | 'addFolderToProject' | 'removeFolderFromProject' | 'openFolder'
>;

/**
 * The home page's TODO list: every TODO in view, by project while several projects are in view
 * and as one list for one project (or their progress, or the deleted TODOs), with the folders of
 * the one project in view under its header, and beside the list the conversation of the TODO
 * selected, or the draft of an import started from the add bar. What is shown is the page's URL;
 * this reads and writes the TODOs through the TODO hooks and draws them with the TODO parts.
 */
export function TodoScreen({
  selection,
  todoList,
  listShowsSelection,
  settings,
  settingsFailed,
  folderActions,
  onSelect,
  onOpenWork,
  onAddProject,
  onWorkspaceChanged,
}: {
  selection: TodoSelection;
  /** Every project's TODOs in the scope, read by the page, whose sidebar counts them too. */
  todoList: TodoListRead;
  /**
   * The page was read for the selected scope and order. Until another selection's read lands,
   * the last page stays on screen, dimmed and closed to writes.
   */
  listShowsSelection: boolean;
  settings: WorkspaceSettings | null;
  /** The workspace could not be read, so the TODOs cannot be put in their projects. */
  settingsFailed: boolean;
  folderActions: ProjectFolderActions;
  onSelect: (next: TodoSelection) => void;
  /** A work named in a conversation opens in the page's main area. */
  onOpenWork: (work: HistorySelection) => void;
  onAddProject: () => void;
  /** An import created organizations or projects: the workspace is read again. */
  onWorkspaceChanged: () => void;
}) {
  const { t } = useI18n();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const importPaneRef = useRef<HTMLDivElement>(null);
  // Measured by the rule between the list and the pane, so held as state to follow remounts.
  const [screenElement, setScreenElement] = useState<HTMLDivElement | null>(null);
  const [listElement, setListElement] = useState<HTMLElement | null>(null);
  const paneWidth = useTodoPaneWidth(screenElement, listElement);
  const importStart = useTodoImportStart();
  const openImports = useOpenTodoImports(selection.scope);
  // How many TODOs the last import added, told over the list once its pane closed.
  const [registered, setRegistered] = useState<number | null>(null);
  const deleted = useDeletedTodos(selection.scope);
  const mutations = useTodoMutations({
    reloadList: todoList.reload,
    reloadDeleted: deleted.reload,
  });
  const { page } = todoList;
  const { failure } = mutations;
  const failureFor = (match: (target: TodoMutationFailure['target']) => boolean) =>
    failure && match(failure.target) ? <FailureLine failure={failure} /> : null;
  const projectName = (projectId: string | null) =>
    settings?.projects.find((project) => project.project_id === projectId)?.display_name ?? null;

  const showingDeleted = selection.view === 'deleted';
  const showingList = selection.view === 'list';
  // An open import takes the pane in place of a TODO's conversation.
  const importId = showingList && page ? selection.importId : null;
  const selectedTodo =
    showingList && importId === null && selection.todoId !== null
      ? (page?.items.find((todo) => todo.todo_id === selection.todoId) ?? null)
      : null;
  const paneOpen = selectedTodo !== null || importId !== null;
  useEffect(() => {
    if (importId !== null) importPaneRef.current?.focus();
  }, [importId]);
  const selectedProjectId =
    selection.project.project === 'project' ? (selection.project.project_id ?? null) : null;
  // One project in view is one list under its name; with several, the groups name them.
  const oneProject = selection.project.project !== 'any';
  const title = !showingList
    ? t('todo.nav.deleted')
    : selectedProjectId !== null
      ? // Named once the workspace is read; the list waits for it too.
        (projectName(selectedProjectId) ?? '')
      : oneProject
        ? t('todo.noProject')
        : t('todo.nav.all');

  // Focus returns to the list: the conversation's close button goes away once used.
  const focusHeading = () => headingRef.current?.focus();
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

  const renderRows = (items: TodoView[], today: string) =>
    items.flatMap((todo) => {
      const rowFailure = failureFor(
        (target) => target.kind === 'todo' && target.todoId === todo.todo_id
      );
      return [
        <TodoRow
          key={todo.todo_id}
          todo={todo}
          today={today}
          selected={todo.todo_id === selectedTodo?.todo_id}
          pending={mutations.pending.has(todo.todo_id)}
          onToggleCheck={() => void mutations.setCheck(todo.todo_id, todo.state.kind !== 'done')}
          onSelect={() => onSelect({ ...selection, todoId: todo.todo_id, importId: null })}
          onDelete={() => void mutations.deleteTodo(todo.todo_id)}
          onSetDates={(change) => void mutations.setDates(todo.todo_id, change)}
        />,
        rowFailure ? (
          <li key={`${todo.todo_id}:failure`} className="todo-screen__failure-row">
            {rowFailure}
          </li>
        ) : null,
      ];
    });

  const renderList = (): ReactNode => {
    if (!page || !settings)
      return (todoList.error && !page) || settingsFailed ? loadFailed : loading;
    return (
      <>
        {/* What was read before the failure stays; the next change reads again. */}
        {todoList.error ? staleAlert : null}
        {/* Without them a draft has no way back in, so their failure is told, unless the TODOs'
            failure already says so; a change reads again. */}
        {openImports.error && !todoList.error ? (
          <p className="history-error" role="alert">
            {t('todo.openImports.loadFailed')}
          </p>
        ) : null}
        {/* A disabled fieldset closes every control in it to the keyboard and the pointer. */}
        <fieldset
          className={
            listShowsSelection
              ? 'todo-screen__todos'
              : 'todo-screen__todos todo-screen__todos--stale'
          }
          disabled={!listShowsSelection}
          aria-busy={!listShowsSelection && !todoList.error}
        >
          <OpenTodoImportRows
            imports={openImports.items}
            openImportId={importId}
            onOpen={(id) => onSelect({ ...selection, todoId: null, importId: id })}
          />
          {groups.length === 0 ? (
            <p className="todo-screen__empty">
              {projects.length === 0 ? t('todo.page.noProjects') : t('todo.page.empty')}
            </p>
          ) : oneProject ? (
            <ul className="todo-list" aria-labelledby="todo-screen-title">
              {renderRows(groups[0].items, page.today)}
            </ul>
          ) : (
            groups.map((group) => (
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
                {renderRows(group.items, page.today)}
              </TodoProjectGroup>
            ))
          )}
        </fieldset>
      </>
    );
  };

  /** How much of what is in view is done, beside the title. */
  const renderSummary = (): ReactNode => {
    const done = groups.reduce((sum, group) => sum + group.doneCount, 0);
    const total = groups.reduce((sum, group) => sum + group.totalCount, 0);
    if (total === 0) return null;
    return (
      <span className="todo-screen__summary">
        {t('todo.list.doneCount', { done, total })}
        <span className="todo-screen__track" aria-hidden="true">
          <span style={{ width: `${(done / total) * 100}%` }} />
        </span>
      </span>
    );
  };

  /** Starts drafting from the add bar's text and files, in the scope in view, and opens it. */
  const startImport = async (text: string, attachments: TodoImportAttachments) => {
    setRegistered(null);
    const started = await importStart.start(selection.scope, text, attachments);
    if (started === null) return false;
    onSelect({ ...selection, todoId: null, importId: started });
    void openImports.reload();
    return true;
  };
  const closeImport = () => {
    onSelect({ ...selection, importId: null });
    focusHeading();
    void openImports.reload();
  };

  /** The bar under the list that adds a TODO. */
  const renderAddBar = (): ReactNode => {
    // A scope with no project yet still imports, which can create its projects.
    if (!page || !settings) return null;
    return (
      <div className="todo-screen__add">
        {failureFor((target) => target.kind === 'add')}
        {importStart.problem ? (
          <p className="todo-screen__failure" role="alert">
            {t(startProblemMessage(importStart.problem))}
          </p>
        ) : null}
        <TodoAddBar
          today={page.today}
          projects={projects}
          disabled={!listShowsSelection}
          onSubmit={mutations.create}
          onImport={startImport}
        />
      </div>
    );
  };

  const renderDeleted = (): ReactNode => {
    const { items } = deleted;
    if (!items || !page) return deleted.error || (todoList.error && !page) ? loadFailed : loading;
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
  // projects' names come with the workspace, so it waits for it as the list does.
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
    <div
      ref={setScreenElement}
      className={paneOpen ? 'todo-screen todo-screen--with-pane' : 'todo-screen'}
      style={paneWidth.style}
    >
      <section
        ref={setListElement}
        className="todo-screen__list"
        aria-labelledby="todo-screen-title"
      >
        <header className="history-pane-header todo-screen__header">
          <h1 id="todo-screen-title" ref={headingRef} tabIndex={-1}>
            {title}
          </h1>
          {showingList ? renderSummary() : null}
          {showingList ? (
            <select
              className="todo-screen__sort"
              aria-label={t('todo.list.sort')}
              value={selection.sort}
              onChange={(event) => {
                const sort = SORTS.find((candidate) => candidate === event.target.value);
                if (sort) onSelect({ ...selection, sort });
              }}
            >
              <option value="added">{t('todo.list.sortAdded')}</option>
              <option value="due">{t('todo.list.sortDue')}</option>
            </select>
          ) : null}
        </header>
        {showingList && settings && selectedProjectId !== null ? (
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
          {showingList && registered !== null ? (
            <p className="todo-screen__notice" role="status">
              {t('todo.import.registered', { count: registered })}
            </p>
          ) : null}
          {showingDeleted ? renderDeleted() : renderList()}
        </div>
        {showingList ? renderAddBar() : null}
      </section>
      {paneOpen ? (
        <div
          className="todo-screen__rule"
          aria-label={t('todo.pane.resize')}
          {...paneWidth.separatorProps}
        />
      ) : null}
      {importId !== null && page ? (
        <div
          ref={importPaneRef}
          className="history-detail todo-screen__conversation todo-screen__import"
          tabIndex={-1}
        >
          <OpenWorkContext.Provider value={onOpenWork}>
            <TodoImportPane
              key={importId}
              importId={importId}
              today={page.today}
              showOrganizations={selection.scope.scope === 'all'}
              onAddProject={onAddProject}
              onRegistered={(count) => {
                closeImport();
                setRegistered(count);
                // The import may have created organizations and projects the TODOs sit in.
                onWorkspaceChanged();
                void todoList.reload();
              }}
              onCanceled={closeImport}
              closeButton={<CloseButton label={t('todo.import.close')} onClose={closeImport} />}
            />
          </OpenWorkContext.Provider>
        </div>
      ) : selectedTodo ? (
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
                <CloseButton
                  label={t('todo.conversation.close')}
                  onClose={() => {
                    onSelect({ ...selection, todoId: null });
                    focusHeading();
                  }}
                />
              }
            />
          </OpenWorkContext.Provider>
        </div>
      ) : null}
    </div>
  );
}
