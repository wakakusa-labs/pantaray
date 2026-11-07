import { useId } from 'react';

import { useI18n } from '@/context/useI18n';

import type {
  GoalProgress as GoalProgressData,
  GoalView,
  ProgressRecordView,
} from '../../../../electron/src/todos/todoContracts';
import { ProgressRecordList, type RecordActions } from './ProgressRecordList';
import {
  formatDate,
  formatPercent,
  formatQuantity,
  goalName,
  momentLocalDate,
  paceOutcome,
  paceRate,
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
  nowLabel,
  expectedLabel,
  targetLabel,
}: {
  share: number;
  expectedShare: number;
  nowLabel: string;
  expectedLabel: string;
  targetLabel: string;
}) {
  const fill = `${Math.min(share, 1) * 100}%`;
  const tick = `${expectedShare * 100}%`;
  return (
    <div className="todo-progress-track">
      {/* Placed over the tick and shifted by the same share of its own width, so it never runs
          past either end. */}
      <div className="todo-progress-track__over">
        <span
          className="todo-progress-track__expected"
          style={{ left: tick, transform: `translateX(-${tick})` }}
        >
          {expectedLabel}
        </span>
      </div>
      <div className="todo-progress-track__bar" aria-hidden="true">
        <i className="todo-progress-track__fill" style={{ width: fill }} />
        <i className="todo-progress-track__tick" style={{ left: tick }} />
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

const goalStatement = (i18n: ProgressI18n, today: string, goal: GoalView) =>
  i18n.t('todoProgress.goal.statement', {
    deadline: formatDate(i18n, today, goal.deadline),
    goal: goalName(i18n, goal),
    setOn: formatDate(i18n, today, momentLocalDate(goal.set_at)),
  });

/**
 * One project's goal and how far its records have come. Only a measured goal shows numbers, and
 * each one opens onto the records it was counted from.
 */
export function GoalProgress({
  progress,
  projectName,
  today,
  recordsById,
  onSetGoal,
  ...recordActions
}: {
  progress: GoalProgressData;
  projectName: string;
  today: string;
  recordsById: ReadonlyMap<string, ProgressRecordView>;
  /** Opens the goal form for a project without one. */
  onSetGoal: () => void;
} & RecordActions) {
  const i18n = useI18n();
  const { t } = i18n;
  const headingId = useId();
  const heading = (
    <h3 id={headingId}>{t('todoProgress.goal.heading', { project: projectName })}</h3>
  );
  if (progress.kind === 'no_goal') {
    return (
      <section className="todo-progress-goal" aria-labelledby={headingId}>
        {heading}
        <p>{t('todoProgress.goal.none')}</p>
        <button
          type="button"
          id={setGoalButtonId(progress.project_id)}
          className="history-filter-button"
          onClick={onSetGoal}
        >
          {t('todoProgress.goal.set')}
        </button>
      </section>
    );
  }

  const { goal } = progress;
  const otherUnit = recordsFor(progress.other_unit_record_ids, recordsById);
  const otherUnitParts =
    otherUnit.length === 0 ? null : (
      <>
        <p>{otherUnitSentence(i18n, otherUnit)}</p>
        <ProgressRecordList
          summary={t('todoProgress.records.otherUnit', {
            records: recordCount(i18n, otherUnit.length),
          })}
          records={otherUnit}
          today={today}
          {...recordActions}
        />
      </>
    );

  if (progress.kind === 'no_records') {
    return (
      <section className="todo-progress-goal" aria-labelledby={headingId}>
        {heading}
        <p className="todo-progress-goal__statement">{goalStatement(i18n, today, goal)}</p>
        <p>
          {otherUnit.length === 0
            ? t('todoProgress.goal.noRecords')
            : t('todoProgress.goal.noRecordsInUnit', {
                unit: t('todoProgress.unitQuoted', { unit: goal.unit }),
              })}
        </p>
        {otherUnitParts}
      </section>
    );
  }

  const counted = recordsFor(progress.record_ids, recordsById);
  const share = formatPercent(i18n, progress.share);
  const total = formatQuantity(i18n, progress.total, goal.unit);
  const { pace } = progress;
  // What leads up to the outcome: a passed deadline, then the pace the outcome comes from.
  const paceLead = [
    progress.deadline_passed && pace.kind !== 'reached'
      ? t('todoProgress.goal.deadlinePassed', { deadline: formatDate(i18n, today, goal.deadline) })
      : null,
    paceRate(i18n, today, pace, goal.unit),
  ].filter((sentence): sentence is string => sentence !== null);
  return (
    <section className="todo-progress-goal" aria-labelledby={headingId}>
      {heading}
      <p className="todo-progress-goal__statement">{goalStatement(i18n, today, goal)}</p>
      <GoalTrack
        share={progress.share}
        expectedShare={progress.expected_share}
        nowLabel={t('todoProgress.goal.now', { share, total })}
        expectedLabel={t('todoProgress.goal.expected', {
          share: formatPercent(i18n, progress.expected_share),
        })}
        targetLabel={t('todoProgress.goal.target', {
          target: formatQuantity(i18n, goal.target, goal.unit),
        })}
      />
      <p>
        {t('todoProgress.goal.counted', {
          share,
          setOn: formatDate(i18n, today, momentLocalDate(goal.set_at)),
          records: recordCount(i18n, counted.length),
          total,
        })}
      </p>
      <p>
        {paceLead.map((sentence) => sentence + t('todoProgress.sentenceSeparator')).join('')}
        <b>{paceOutcome(i18n, pace)}</b>
      </p>
      <ProgressRecordList
        summary={t('todoProgress.records.counted', { records: recordCount(i18n, counted.length) })}
        records={counted}
        today={today}
        {...recordActions}
      />
      {otherUnitParts}
    </section>
  );
}
