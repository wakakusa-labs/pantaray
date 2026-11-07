import { useI18n } from '@/context/useI18n';

import type { TodoScopeQuery } from '../../../../electron/src/todos/todoContracts';
import { useTodoProgress } from '../../../todos/useTodoProgress';
import { useTodoProgressMutations } from '../../../todos/useTodoProgressMutations';
import { GoalForm } from './GoalForm';

/** A project's goal is the same in every scope its TODOs are read in. */
const ALL_SCOPE: TodoScopeQuery = { scope: 'all' };

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
  const { t } = useI18n();
  // The period only shapes the rows, which the form does not show.
  const progress = useTodoProgress(
    ALL_SCOPE,
    { project: 'project', project_id: projectId },
    'week'
  );
  const mutations = useTodoProgressMutations({ reloadProgress: progress.reload });
  const { page } = progress;
  // The local read answers at once; until then there is nothing to fill the form with.
  if (!page && !progress.error) return null;
  // Asked for one project, the page reports that project's goal alone, with or without one.
  const [reported] = page?.goals ?? [];
  const goal = reported === undefined || reported.kind === 'no_goal' ? null : reported.goal;
  return (
    <GoalForm
      projectName={projectName}
      goal={goal}
      // Without the goal it has now, the form still sets the one written in it.
      failure={mutations.failure?.message ?? (page ? null : t('todoProgress.form.readFailed'))}
      onSave={(input) => mutations.setGoal(projectId, input)}
      onClear={() => mutations.clearGoal(projectId)}
      onClose={onClose}
    />
  );
}
