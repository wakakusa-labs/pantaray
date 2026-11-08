import { useState } from 'react';
import { flushSync } from 'react-dom';

import { useI18n } from '@/context/useI18n';

import type { TodoScopeQuery } from '../../../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from '../../../todos/todoSelection';
import { useTodoProgress, type ProgressPeriod } from '../../../todos/useTodoProgress';
import { useTodoProgressMutations } from '../../../todos/useTodoProgressMutations';
import { GoalForm } from './GoalForm';
import { ProgressView } from './ProgressView';
import { setGoalButtonId, type ProjectNameOf } from './progressText';

/**
 * The TODO page's progress view: reads the progress of the TODOs in view and writes its records
 * and the goal of a project without one. Each number's records open their conversation.
 */
export function ProgressPane({
  scope,
  projectFilter,
  period,
  projectName,
  onPeriodChange,
  onOpenEvidence,
  onFocusLost,
}: {
  scope: TodoScopeQuery;
  projectFilter: TodoProjectFilter;
  period: ProgressPeriod;
  projectName: ProjectNameOf;
  onPeriodChange: (period: ProgressPeriod) => void;
  /** Opens the conversation whose results a record came from. */
  onOpenEvidence: (actionId: string) => void;
  /** Focus has nowhere to return: the goal saved, its 「目標を設定」 is gone. */
  onFocusLost: () => void;
}) {
  const { t } = useI18n();
  const progress = useTodoProgress(scope, projectFilter, period, null);
  const mutations = useTodoProgressMutations({ reloadProgress: progress.reload });
  const [goalFormFor, setGoalFormFor] = useState<string | null>(null);
  const { page } = progress;
  const { failure } = mutations;

  if (!page) {
    return progress.error ? (
      <p className="history-error" role="alert">
        {t('todoProgress.loadFailed')}
      </p>
    ) : (
      <p className="history-loading" role="status">
        {t('todoProgress.loading')}
      </p>
    );
  }

  // A modal dialog holds focus until it is gone, so it closes before focus moves.
  const closeGoalForm = (projectId: string) => {
    flushSync(() => setGoalFormFor(null));
    const button = document.getElementById(setGoalButtonId(projectId));
    if (button) button.focus();
    else onFocusLost();
  };
  return (
    <>
      {/* What was read before the failure stays; the next change reads again. */}
      {progress.error ? (
        <p className="history-error" role="alert">
          {t('todoProgress.loadFailedStale')}
        </p>
      ) : null}
      <ProgressView
        page={page}
        projectName={projectName}
        onPeriodChange={onPeriodChange}
        onSetGoal={setGoalFormFor}
        pendingRecordIds={mutations.pendingRecordIds}
        recordFailure={
          failure?.target.kind === 'record'
            ? { recordId: failure.target.recordId, message: failure.message }
            : null
        }
        onOpenEvidence={onOpenEvidence}
        onDeleteRecord={(recordId) => void mutations.deleteRecord(recordId)}
      />
      {goalFormFor !== null ? (
        <GoalForm
          projectName={projectName(goalFormFor)}
          // Only a project without a goal offers its form here.
          goal={null}
          failure={
            failure?.target.kind === 'goal' && failure.target.projectId === goalFormFor
              ? failure.message
              : null
          }
          onSave={(goal) => mutations.setGoal(goalFormFor, goal)}
          onClear={() => mutations.clearGoal(goalFormFor)}
          onClose={() => closeGoalForm(goalFormFor)}
        />
      ) : null}
    </>
  );
}
