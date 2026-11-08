import { useI18n } from '@/context/useI18n';

import type {
  GoalProgress as GoalProgressData,
  GoalView,
  ProgressRecordView,
} from '../../../../electron/src/todos/todoContracts';
import {
  formatDate,
  formatPercent,
  formatQuantity,
  goalName,
  lateSentence,
  missesDeadline,
  paceOutcome,
  recordCount,
  recordsFor,
  setGoalButtonId,
  type ProgressI18n,
} from './progressText';

import './progress.css';

/** The share so far on a thin track, with a tick where a straight line to the deadline is now. */
function GoalTrack({
  share,
  expectedShare,
  expectedLabel,
  nowLabel,
  targetLabel,
}: {
  share: number;
  expectedShare: number;
  expectedLabel: string;
  nowLabel: string;
  targetLabel: string;
}) {
  return (
    <div className="todo-progress-track">
      <div className="todo-progress-track__bar">
        <i
          className="todo-progress-track__fill"
          style={{ width: `${Math.min(share, 1) * 100}%` }}
        />
        <i
          className="todo-progress-track__tick"
          style={{ left: `${Math.min(expectedShare, 1) * 100}%` }}
          title={expectedLabel}
        />
        <span className="todo-progress-sr-only">{expectedLabel}</span>
      </div>
      <div className="todo-progress-track__under">
        <span>{nowLabel}</span>
        <span>{targetLabel}</span>
      </div>
    </div>
  );
}

/** 「単位が「件」の記録 2 件は、合計に入れていません。」, naming each unit once. */
function otherUnitSentence(i18n: ProgressI18n, records: readonly ProgressRecordView[]) {
  const units = [...new Set(records.map((record) => record.unit))]
    .map((unit) => i18n.t('todoProgress.unitQuoted', { unit }))
    .join(i18n.t('todoProgress.listSeparator'));
  return i18n.t('todoProgress.goal.otherUnits', {
    units,
    records: recordCount(i18n, records.length),
  });
}

/** The goal's name over its project and deadline. */
function GoalName({
  goal,
  projectName,
  today,
}: {
  goal: GoalView;
  projectName: string;
  today: string;
}) {
  const i18n = useI18n();
  return (
    <div className="todo-progress-goal__name">
      <span className="todo-progress-dot" aria-hidden="true" />
      <div>
        <b>{goalName(i18n, goal)}</b>
        <small>
          <span>{projectName}</span>
          <span>
            {i18n.t('todoProgress.goal.deadline', {
              deadline: formatDate(i18n, today, goal.deadline),
            })}
          </span>
        </small>
      </div>
    </div>
  );
}

/**
 * One project's goal on one ruled row: its name, a track of how far its records have come, and
 * the share with what the pace says. Only a measured goal shows numbers; the pace is ochre only
 * when it misses the deadline.
 */
export function GoalProgress({
  progress,
  projectName,
  today,
  recordsById,
  onSetGoal,
}: {
  progress: GoalProgressData;
  projectName: string;
  today: string;
  recordsById: ReadonlyMap<string, ProgressRecordView>;
  /** Opens the goal form for a project without one. */
  onSetGoal: () => void;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const label = t('todoProgress.goal.heading', { project: projectName });
  if (progress.kind === 'no_goal') {
    return (
      <section className="todo-progress-goal" aria-label={label}>
        <div className="todo-progress-goal__name">
          <span className="todo-progress-dot" aria-hidden="true" />
          <div>
            <b>{projectName}</b>
            <small>{t('todoProgress.goal.none')}</small>
          </div>
        </div>
        <span />
        <div className="todo-progress-goal__pace">
          <button
            type="button"
            id={setGoalButtonId(progress.project_id)}
            className="history-filter-button todo-progress-goal__set"
            onClick={onSetGoal}
          >
            {t('todoProgress.goal.set')}
          </button>
        </div>
      </section>
    );
  }

  const { goal } = progress;
  const otherUnit = recordsFor(progress.other_unit_record_ids, recordsById);
  const otherUnitNote =
    otherUnit.length === 0 ? null : <small>{otherUnitSentence(i18n, otherUnit)}</small>;

  if (progress.kind === 'no_records') {
    return (
      <section className="todo-progress-goal" aria-label={label}>
        <GoalName goal={goal} projectName={projectName} today={today} />
        <p className="todo-progress-goal__empty">
          {otherUnit.length === 0
            ? t('todoProgress.goal.noRecords')
            : t('todoProgress.goal.noRecordsInUnit', {
                unit: t('todoProgress.unitQuoted', { unit: goal.unit }),
              })}
        </p>
        <div className="todo-progress-goal__pace">{otherUnitNote}</div>
      </section>
    );
  }

  const { pace } = progress;
  const late = (progress.deadline_passed && pace.kind !== 'reached') || missesDeadline(progress);
  const paceText = [
    progress.deadline_passed && pace.kind !== 'reached'
      ? t('todoProgress.goal.deadlinePassed', { deadline: formatDate(i18n, today, goal.deadline) })
      : null,
    paceOutcome(i18n, pace),
    missesDeadline(progress) ? lateSentence(i18n, today, goal) : null,
  ]
    .filter((sentence): sentence is string => sentence !== null)
    .join(t('todoProgress.sentenceSeparator'));
  return (
    <section className="todo-progress-goal" aria-label={label}>
      <GoalName goal={goal} projectName={projectName} today={today} />
      <GoalTrack
        share={progress.share}
        expectedShare={progress.expected_share}
        expectedLabel={t('todoProgress.goal.expected', {
          share: formatPercent(i18n, progress.expected_share),
        })}
        nowLabel={formatQuantity(i18n, progress.total, goal.unit)}
        targetLabel={formatQuantity(i18n, goal.target, goal.unit)}
      />
      <div className="todo-progress-goal__pace">
        <b>{formatPercent(i18n, progress.share)}</b>
        <span className={late ? 'todo-progress-goal__late' : undefined}>{paceText}</span>
        {otherUnitNote}
      </div>
    </section>
  );
}
