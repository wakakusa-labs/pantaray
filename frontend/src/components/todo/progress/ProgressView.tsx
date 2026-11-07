import { useId, useMemo } from 'react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type { TodoProgressPage } from '../../../../electron/src/todos/todoContracts';
import type { ProgressPeriod } from '../../../todos/useTodoProgress';
import { GoalProgress } from './GoalProgress';
import { ProgressPeriodRow } from './ProgressPeriodRow';
import type { RecordActions } from './ProgressRecordList';
import { leadSentence, type ProjectNameOf } from './progressText';

import './progress.css';

const PERIODS: readonly ProgressPeriod[] = ['day', 'week', 'month'];
const PERIOD_LABELS: Readonly<Record<ProgressPeriod, MessageKey>> = {
  day: 'todoProgress.period.day',
  week: 'todoProgress.period.week',
  month: 'todoProgress.period.month',
};

/**
 * The progress of the TODOs in view: the period switch, one sentence on the newest period, each
 * goal's block, and a row per period. Every number on it is a count, sum or ratio of records
 * and TODOs the page carries, and opens onto them.
 */
export function ProgressView({
  page,
  projectName,
  onPeriodChange,
  ...recordActions
}: {
  page: TodoProgressPage;
  projectName: ProjectNameOf;
  onPeriodChange: (period: ProgressPeriod) => void;
} & RecordActions) {
  const i18n = useI18n();
  const { t } = i18n;
  const headingId = useId();
  const rowsHeadingId = useId();
  const recordsById = useMemo(
    () => new Map(page.records.map((record) => [record.record_id, record])),
    [page.records]
  );
  const maxRecordCount = Math.max(1, ...page.rows.map((row) => row.record_ids.length));
  return (
    <section className="todo-progress" aria-labelledby={headingId}>
      <div className="todo-progress__head">
        <h2 id={headingId}>{t('todoProgress.title')}</h2>
        <div
          className="todo-progress__periods"
          role="group"
          aria-label={t('todoProgress.period.label')}
        >
          {PERIODS.map((period) => (
            <button
              key={period}
              type="button"
              className="history-filter-button todo-progress__period"
              aria-pressed={period === page.period}
              onClick={() => onPeriodChange(period)}
            >
              {t(PERIOD_LABELS[period])}
            </button>
          ))}
        </div>
      </div>
      <p className="todo-progress__lead" aria-live="polite">
        {leadSentence(i18n, page, recordsById, projectName)}
      </p>
      {page.goals.length === 0 ? (
        <section className="todo-progress-goal">
          <h3>{t('todoProgress.goal.headingAny')}</h3>
          <p>{t('todoProgress.goal.none')}</p>
        </section>
      ) : (
        page.goals.map((progress) => {
          const projectId =
            progress.kind === 'no_goal' ? progress.project_id : progress.goal.project_id;
          return (
            <GoalProgress
              key={projectId}
              progress={progress}
              projectName={projectName(projectId)}
              today={page.today}
              recordsById={recordsById}
              {...recordActions}
            />
          );
        })
      )}
      <section className="todo-progress-rows" aria-labelledby={rowsHeadingId}>
        <h3 id={rowsHeadingId}>{t(PERIOD_LABELS[page.period])}</h3>
        <ul>
          {page.rows.map((row) => (
            <ProgressPeriodRow
              key={row.start}
              row={row}
              period={page.period}
              today={page.today}
              recordsById={recordsById}
              projectName={projectName}
              maxRecordCount={maxRecordCount}
              {...recordActions}
            />
          ))}
        </ul>
      </section>
    </section>
  );
}
