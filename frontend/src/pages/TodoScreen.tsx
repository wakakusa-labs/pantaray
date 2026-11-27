import { Maximize2, Minimize2, X } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import { PageControlButton } from '@/components/task/PageControlButton';
import { DeletedTodoRow } from '@/components/todo/DeletedTodoRow';
import { OpenTodoImportRows } from '@/components/todo/OpenTodoImportRows';
import { ProgressScreen } from '@/components/todo/progress/ProgressScreen';
import { TodoConversationPane } from '@/components/todo/TodoConversationPane';
import { TodoImportBar } from '@/components/todo/TodoImportBar';
import { TodoImportPane } from '@/components/todo/TodoImportPane';
import { TodoNewLine } from '@/components/todo/TodoNewLine';
import { TodoProjectGroup } from '@/components/todo/TodoProjectGroup';
import { TodoRow } from '@/components/todo/TodoRow';
import { useTodoDeleteConfirmation } from '@/components/todo/useTodoDeleteConfirmation';
import { useTodoPaneWidth } from '@/components/todo/useTodoPaneWidth';
import { useI18n } from '@/context/useI18n';
import type { TaskPreview } from '@/components/task/taskFiles';
import type { HistorySelection } from '@/history/historySelection';
import type { WorkspaceSettings } from '@/pages/settings/components/workspaceSettingsModel';
import {
  groupTodosByProject,
  todoProjectSlots,
  type TodoPlace,
  type TodoProjectGroupView,
  type TodoProjectSlot,
} from '@/todos/groupTodosByProject';
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

/** A failed write, told under the row or the new line it was made from. */
function FailureLine({ failure }: { failure: TodoMutationFailure }) {
  return (
    <p className="todo-screen__failure" role="alert">
      {failure.message}
    </p>
  );
}

/** The list, which takes focus once a pane closes; the page focuses it too. */
export const TODO_SCREEN_LIST_ID = 'todo-screen-list';

/** Why an import did not start, told under the import bar. */
const startProblemMessage = (problem: TodoImportProblem) =>
  problem === 'files_unavailable' || problem === 'images_invalid'
    ? 'todo.import.filesUnavailable'
    : 'todo.import.startFailed';

/** One key per place: a project, or an organization's own no project, or none. */
const placeKey = (place: TodoPlace) =>
  place.projectId ?? `organization:${place.organizationId ?? ''}`;

/**
 * The home page's TODO list: every TODO in view, by project while several projects are in view
 * and as one list for one project (or their progress, or the deleted TODOs), each list ending in
 * the line a TODO is written on, and beside the list the conversation of the TODO selected, or
 * the draft of an import started from the import bar. The
 * conversation expands into the list's place and shrinks back beside it, the same pane all along.
 * What is shown is the page's URL; this reads and writes the TODOs through the TODO hooks and
 * draws them with the TODO parts.
 */
export function TodoScreen({
  selection,
  todoList,
  listShowsSelection,
  settings,
  settingsFailed,
  onSelect,
  onOpenWork,
  onAddProject,
  onWorkspaceChanged,
  requestedFile,
  handedPreview,
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
  onSelect: (next: TodoSelection) => void;
  /** A work named in a conversation opens in the page's main area. */
  onOpenWork: (work: HistorySelection) => void;
  onAddProject: () => void;
  /** An import created organizations or projects: the workspace is read again. */
  onWorkspaceChanged: () => void;
  /** A file of the selected TODO's suggestion an Overlay chip asked to preview. */
  requestedFile: string | null;
  /** The file a suggestion had open, handed to the Action that took its place. */
  handedPreview: { actionId: string; preview: TaskPreview } | null;
}) {
  const { t } = useI18n();
  const importPaneRef = useRef<HTMLDivElement>(null);
  // Measured by the rule between the list and the pane, so held as state to follow remounts.
  const [screenElement, setScreenElement] = useState<HTMLDivElement | null>(null);
  const [listElement, setListElement] = useState<HTMLElement | null>(null);
  const paneWidth = useTodoPaneWidth(screenElement, listElement);
  const importStart = useTodoImportStart();
  const openImports = useOpenTodoImports(selection.scope);
  // How many TODOs the last import added, told over the list once its pane closed.
  const [registered, setRegistered] = useState<number | null>(null);
  // The place of the last add, under whose new line its failure is told.
  const [addedTo, setAddedTo] = useState<string | null>(null);
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
  // The conversation takes the list's place, the same pane, so it is not mounted again.
  const expanded = selection.expanded && selectedTodo !== null;
  useEffect(() => {
    if (importId !== null) importPaneRef.current?.focus();
  }, [importId]);
  const selectedProjectId =
    selection.project.project === 'project' ? (selection.project.project_id ?? null) : null;
  // One project in view is one list under its name; with several, the groups name them. No
  // project across all organizations is a group for each organization.
  const oneProject = selection.project.project !== 'any';
  // The list's name for assistive tech. No title is drawn: the sidebar shows what is selected,
  // and the groups name their projects.
  const listName = !showingList
    ? t('todo.nav.deleted')
    : selectedProjectId !== null
      ? // Named once the workspace is read; the list waits for it too.
        (projectName(selectedProjectId) ?? '')
      : oneProject
        ? t('todo.noProject')
        : t('todo.nav.all');

  // Focus returns to the list: the pane's close button goes away once used. An expanded
  // conversation's list is hidden until the closing selection lands, so the focus waits for it.
  const listFocusPending = useRef(false);
  useEffect(() => {
    if (!listFocusPending.current || !listElement || listElement.hidden) return;
    listFocusPending.current = false;
    listElement.focus();
  });
  const focusList = () => {
    listFocusPending.current = true;
  };
  const deletion = useTodoDeleteConfirmation({
    deleteTodo: mutations.deleteTodo,
    onDeleted: focusList,
  });
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
  // An empty list is written into at the first place in view, drawn as the group it will be, so
  // the line stays where it is once its first TODO is added. No place: nothing can be added.
  const shown: TodoProjectGroupView[] =
    groups.length > 0 || projects.length === 0
      ? groups
      : [{ ...projects[0], doneCount: 0, totalCount: 0, items: [] }];

  /** The place's name: its project, or no project in its organization while those are named. */
  const placeName = (slot: TodoProjectSlot) =>
    slot.projectName ??
    (slot.organization === null
      ? t('todo.noProject')
      : t('todo.noProjectIn', {
          organization:
            slot.organization.kind === 'organization'
              ? slot.organization.name
              : t('todo.rail.none'),
        }));

  const todoRows = (items: TodoView[], today: string) =>
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
          onDelete={() => void deletion.requestDelete(todo)}
          onSetDates={(change) => void mutations.setDates(todo.todo_id, change)}
        />,
        rowFailure ? (
          <li key={`${todo.todo_id}:failure`} className="todo-screen__failure-row">
            {rowFailure}
          </li>
        ) : null,
      ];
    });

  /** A group's rows, then the line the next TODO in its place is written on. */
  const renderRows = (group: TodoProjectGroupView, today: string) => {
    const place = placeKey(group);
    const addFailure = addedTo === place ? failureFor((target) => target.kind === 'add') : null;
    return [
      ...todoRows(group.items, today),
      <TodoNewLine
        key="new"
        placeName={placeName(group)}
        onCreate={(text) => {
          setAddedTo(place);
          return mutations.create(
            { projectId: group.projectId, organizationId: group.organizationId },
            today,
            text,
            null
          );
        }}
      />,
      addFailure ? (
        <li key="new:failure" className="todo-screen__failure-row">
          {addFailure}
        </li>
      ) : null,
    ];
  };

  const renderList = (): ReactNode => {
    if (!page || !settings)
      return (todoList.error && !page) || settingsFailed ? loadFailed : loading;
    return (
      <>
        {/* What was read before the failure stays; the next change reads again. */}
        {todoList.error ? staleAlert : null}
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
          {/* An empty list that can be written into shows its new line instead. */}
          {shown.length === 0 ? <p className="todo-screen__empty">{t('todo.page.empty')}</p> : null}
          {oneProject && shown.length === 1 ? (
            <ul className="todo-list" aria-label={listName}>
              {renderRows(shown[0], page.today)}
            </ul>
          ) : (
            shown.map((group) => (
              <TodoProjectGroup
                key={placeKey(group)}
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
                {renderRows(group, page.today)}
              </TodoProjectGroup>
            ))
          )}
        </fieldset>
      </>
    );
  };

  /** How much of what is in view is done, in the header. */
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

  /** Starts drafting from the import bar's text and files, in the scope in view, and opens it. */
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
    focusList();
    void openImports.reload();
  };

  /** The bar under the list that imports TODOs. */
  const renderImportBar = (): ReactNode => {
    // A scope with no project yet still imports, which can create its projects.
    if (!page || !settings) return null;
    return (
      <div className="todo-screen__add">
        {importStart.problem ? (
          <p className="todo-screen__failure" role="alert">
            {t(startProblemMessage(importStart.problem))}
          </p>
        ) : null}
        <TodoImportBar disabled={!listShowsSelection} onImport={startImport} />
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
      className={
        expanded
          ? 'todo-screen todo-screen--expanded'
          : paneOpen
            ? 'todo-screen todo-screen--with-pane'
            : 'todo-screen'
      }
      style={paneWidth.style}
    >
      {/* Hidden while the conversation is expanded, as it was left: its scroll and the text of
          its new lines and import bar are there again on shrinking. */}
      <section
        ref={setListElement}
        id={TODO_SCREEN_LIST_ID}
        className="todo-screen__list"
        aria-label={listName}
        tabIndex={-1}
        hidden={expanded}
      >
        <header className="history-pane-header todo-screen__header">
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
        <div className="todo-screen__scroll">
          <AiConnectionNotice />
          {showingList && registered !== null ? (
            <p className="todo-screen__notice" role="status">
              {t('todo.import.registered', { count: registered })}
            </p>
          ) : null}
          {showingDeleted ? renderDeleted() : renderList()}
        </div>
        {showingList ? renderImportBar() : null}
      </section>
      {deletion.dialog}
      {paneOpen && !expanded ? (
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
              pageControls={
                <PageControlButton label={t('todo.import.close')} icon={X} onClick={closeImport} />
              }
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
              requestedFile={requestedFile}
              handedPreview={handedPreview}
              pageControls={
                <>
                  {/* One button that turns over, so the focus stays on it. */}
                  <PageControlButton
                    label={t(expanded ? 'todo.conversation.shrink' : 'todo.conversation.expand')}
                    icon={expanded ? Minimize2 : Maximize2}
                    onClick={() => onSelect({ ...selection, expanded: !expanded })}
                  />
                  <PageControlButton
                    label={t('todo.conversation.close')}
                    icon={X}
                    onClick={() => {
                      onSelect({ ...selection, todoId: null, expanded: false });
                      focusList();
                    }}
                  />
                </>
              }
            />
          </OpenWorkContext.Provider>
        </div>
      ) : null}
    </div>
  );
}
