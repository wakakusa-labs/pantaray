import { useEffect, useId, useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';

import type { TodoScopeQuery } from '../../../../electron/src/todos/todoContracts';
import { useTodoProgress } from '../../../todos/useTodoProgress';
import { useTodoProgressMutations } from '../../../todos/useTodoProgressMutations';
import { GoalForm } from './GoalForm';

import './progress.css';

/** A project's goal is the same in every scope its TODOs are read in. */
const ALL_SCOPE: TodoScopeQuery = { scope: 'all' };

/**
 * The project's goal could not be read. A save would replace a goal the user has not seen (the
 * backend sets or edits alike), so nothing can be written until a read goes through.
 */
function GoalReadFailed({
  projectName,
  onRetry,
  onClose,
}: {
  projectName: string;
  onRetry: () => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const bodyId = useId();
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return (
    <dialog
      ref={dialogRef}
      className="todo-goal-form"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id={titleId}>{t('todoProgress.goal.heading', { project: projectName })}</h2>
      <p id={bodyId} className="todo-goal-form__failure" role="alert">
        {t('todoProgress.form.readFailed')}
      </p>
      <div className="todo-goal-form__actions">
        <button type="button" className="history-filter-button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="history-filter-button"
          disabled={retrying}
          onClick={() => {
            setRetrying(true);
            // A read that goes through replaces this dialog with the form.
            void onRetry().finally(() => setRetrying(false));
          }}
        >
          {t('todoProgress.form.retryRead')}
        </button>
      </div>
    </dialog>
  );
}

/**
 * The goal form for a project named outside the progress view, opened on the goal the project
 * has now, which it reads for itself. Mounted only while open, as the form is.
 */
export function ProjectGoalDialog({
  projectId,
  projectName,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  // The period only shapes the rows, which the form does not show.
  const progress = useTodoProgress(
    ALL_SCOPE,
    { project: 'project', project_id: projectId },
    'week'
  );
  const mutations = useTodoProgressMutations({ reloadProgress: progress.reload });
  const { page } = progress;
  if (!page) {
    // The local read answers at once; until then there is nothing to fill the form with.
    if (!progress.error) return null;
    return <GoalReadFailed projectName={projectName} onRetry={progress.reload} onClose={onClose} />;
  }
  // Asked for one project, the page reports that project's goal alone, with or without one.
  const [reported] = page.goals;
  const goal = reported === undefined || reported.kind === 'no_goal' ? null : reported.goal;
  return (
    <GoalForm
      projectName={projectName}
      goal={goal}
      failure={mutations.failure?.message ?? null}
      onSave={(input) => mutations.setGoal(projectId, input)}
      onClear={() => mutations.clearGoal(projectId)}
      onClose={onClose}
    />
  );
}
