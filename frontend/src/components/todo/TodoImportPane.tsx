import { useId, type ReactNode } from 'react';

import { ActionTaskPane } from '@/components/task/ActionTaskPane';
import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';
import { editDraftTodo, registeredCount, removeDraftTodo } from '@/todos/todoImportDraft';
import { useTodoImport, type TodoImportProblem } from '@/todos/useTodoImport';

import type { TodoImportView } from '../../../electron/src/todos/todoImportContracts';
import { TodoImportDraftTree } from './TodoImportDraftTree';

import './todoImport.css';

type DraftIssueReason = NonNullable<TodoImportView['issue']>['reason'];

const ISSUE_MESSAGES: Record<DraftIssueReason, MessageKey> = {
  organization_not_found: 'todo.import.issue.organization_not_found',
  organization_outside_scope: 'todo.import.issue.organization_outside_scope',
  project_required: 'todo.import.issue.project_required',
  project_name_taken: 'todo.import.issue.project_name_taken',
  invalid_text: 'todo.import.issue.invalid_text',
  too_many_todos: 'todo.import.issue.too_many_todos',
};

const isIssueReason = (problem: TodoImportProblem): problem is DraftIssueReason =>
  Object.prototype.hasOwnProperty.call(ISSUE_MESSAGES, problem);

/**
 * What a refused or unanswered request tells beyond the draft read after it. A draft issue is
 * told with the draft's own; a closed import says so in place of its buttons.
 */
function problemMessage(problem: TodoImportProblem | null): MessageKey | null {
  if (problem === null || isIssueReason(problem)) return null;
  if (problem === 'registered' || problem === 'canceled') return null;
  if (problem === 'stale_revision') return 'todo.import.stale';
  if (problem === 'not_found') return 'todo.import.missing';
  return 'todo.import.failed';
}

/**
 * A TODO import beside the list: the conversation with the Action drafting it, where the user
 * asks for changes, and under it the draft, which the user also edits directly, registers or
 * cancels. While the Action runs, the draft is its to write, so direct edits and 登録 wait.
 */
export function TodoImportPane({
  importId,
  today,
  showOrganizations,
  closeButton,
  onAddProject,
  onRegistered,
  onCanceled,
}: {
  importId: string;
  /** The backend's today, which the date pickers count from. */
  today: string;
  /** Every organization is in view, so the draft may create organizations and names them. */
  showOrganizations: boolean;
  closeButton: ReactNode;
  onAddProject: () => void;
  /** The import created this many TODOs. */
  onRegistered: (count: number) => void;
  onCanceled: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const title = t('todo.import.title');
  const draft = useTodoImport(importId);
  const { view } = draft;

  if (view === null || view.action_id === null) {
    const message = draft.missing
      ? t('todo.import.missing')
      : view !== null
        ? t('todo.import.preparing')
        : draft.error
          ? t('todo.import.loadFailed')
          : t('todo.import.loading');
    return (
      <section className="todo-conversation-none" aria-labelledby={titleId}>
        <header className="action-task__header">
          <h1 id={titleId}>{title}</h1>
          <div className="action-task__header-actions">{closeButton}</div>
        </header>
        <p
          className="todo-conversation-none__text"
          role={draft.missing || draft.error ? 'alert' : 'status'}
        >
          {message}
        </p>
      </section>
    );
  }

  const renderDraft = (running: boolean) => {
    const closed = view.state !== 'open';
    const count = registeredCount(view.groups);
    const issue = view.issue?.reason ?? null;
    const problem = problemMessage(draft.problem);
    return (
      <div className="todo-import">
        {view.groups.length > 0 ? (
          <TodoImportDraftTree
            groups={view.groups}
            showOrganizations={showOrganizations}
            fieldsKey={`${view.revision}:${draft.discards}`}
            today={today}
            // An edit that did not land is saved again or let go before the next one.
            disabled={running || draft.pending || closed || draft.unsaved}
            onEdit={(at, patch) => void draft.write(editDraftTodo(view.groups, at, patch))}
            onRemove={(at) => void draft.write(removeDraftTodo(view.groups, at))}
          />
        ) : running ? null : (
          <p className="todo-import__note">{t('todo.import.empty')}</p>
        )}
        {closed ? (
          <p className="todo-import__note">
            {t(
              view.state === 'registered'
                ? 'todo.import.state.registered'
                : 'todo.import.state.canceled'
            )}
          </p>
        ) : (
          <>
            {running ? (
              <p className="todo-import__note" role="status">
                {t('todo.import.running')}
              </p>
            ) : null}
            {issue ? (
              <p className="todo-import__alert" role="alert">
                {t(ISSUE_MESSAGES[issue])}
              </p>
            ) : null}
            {draft.unsaved ? (
              <div className="todo-import__unsaved">
                <p className="todo-import__alert" role="alert">
                  {t('todo.import.unsaved')}
                  {draft.problem && isIssueReason(draft.problem)
                    ? ` ${t(ISSUE_MESSAGES[draft.problem])}`
                    : null}
                </p>
                <button
                  type="button"
                  className="history-filter-button"
                  disabled={draft.pending}
                  onClick={() => void draft.saveAgain()}
                >
                  {t('todo.import.saveAgain')}
                </button>
                <button
                  type="button"
                  className="history-filter-button"
                  disabled={draft.pending}
                  onClick={draft.discardUnsaved}
                >
                  {t('todo.import.discard')}
                </button>
              </div>
            ) : problem ? (
              <p className="todo-import__alert" role="alert">
                {t(problem)}
              </p>
            ) : null}
            <div className="todo-import__actions">
              <button
                type="button"
                className="history-filter-button"
                disabled={draft.pending}
                onClick={async () => {
                  if (await draft.cancel()) onCanceled();
                }}
              >
                {t('todo.import.cancel')}
              </button>
              <button
                type="button"
                className="history-filter-button todo-import__register"
                disabled={
                  running || draft.pending || draft.unsaved || issue !== null || count === 0
                }
                onClick={async () => {
                  const registered = await draft.register();
                  if (registered) onRegistered(registered.result?.todo_ids.length ?? count);
                }}
              >
                {t('todo.import.register', { count })}
              </button>
            </div>
          </>
        )}
      </div>
    );
  };

  return (
    <ActionTaskPane
      key={view.action_id}
      actionId={view.action_id}
      title={title}
      onAddProject={onAddProject}
      closeButton={closeButton}
      renderUnderConversation={renderDraft}
    />
  );
}
