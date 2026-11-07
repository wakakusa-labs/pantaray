import { useI18n } from '@/context/useI18n';

import type {
  ProgressPeriodView,
  ProgressRecordView,
} from '../../../../electron/src/todos/todoContracts';
import type { ProgressPeriod } from '../../../todos/useTodoProgress';
import { ProgressRecordList, type RecordActions } from './ProgressRecordList';
import {
  countByProject,
  recordCount,
  formatDate,
  formatMonth,
  formatWeekday,
  projectLabel,
  recordsFor,
  type ProgressI18n,
  type ProjectNameOf,
} from './progressText';

import './progress.css';

/** 「11/7（土）」, 「11/2 の週」, 「11 月（11/7 まで）」 for the month still going. */
function periodLabel(
  i18n: ProgressI18n,
  period: ProgressPeriod,
  today: string,
  row: ProgressPeriodView
): string {
  const { t, language } = i18n;
  const date = formatDate(i18n, today, row.start);
  if (period === 'day') {
    return t('todoProgress.row.day', { date, weekday: formatWeekday(language, row.start) });
  }
  if (period === 'week') return t('todoProgress.row.week', { date });
  const month = formatMonth(i18n, row.start);
  return row.end > today
    ? t('todoProgress.row.monthSoFar', { month, date: formatDate(i18n, today, today) })
    : month;
}

/**
 * One day, week or month: how many records each project gained, how many TODOs were checked,
 * and those TODOs' own words. The bar is the row's record count against the busiest row's. The
 * count opens onto its records, with or without a goal or a project to count them toward.
 */
export function ProgressPeriodRow({
  row,
  period,
  today,
  recordsById,
  projectName,
  maxRecordCount,
  ...recordActions
}: {
  row: ProgressPeriodView;
  period: ProgressPeriod;
  today: string;
  recordsById: ReadonlyMap<string, ProgressRecordView>;
  projectName: ProjectNameOf;
  /** The most records any row in view has; at least 1. */
  maxRecordCount: number;
} & RecordActions) {
  const i18n = useI18n();
  const { t } = i18n;
  const separator = t('todoProgress.listSeparator');
  const records = recordsFor(row.record_ids, recordsById);
  const counts = countByProject(records);
  return (
    <li className="todo-progress-row">
      <span className="todo-progress-row__label">{periodLabel(i18n, period, today, row)}</span>
      <span className="todo-progress-row__bar" aria-hidden="true">
        <i style={{ width: `${(row.record_ids.length / maxRecordCount) * 100}%` }} />
      </span>
      <span className="todo-progress-row__records">
        {counts.length === 0
          ? t('todoProgress.row.noRecords')
          : counts
              .map(({ projectId, count }) =>
                t('todoProgress.row.project', {
                  project: projectLabel(i18n, projectName, projectId),
                  count,
                })
              )
              .join(separator)}
      </span>
      <span className="todo-progress-row__checked">
        {t('todoProgress.row.checked', { count: row.checked.length })}
      </span>
      {row.checked.length > 0 ? (
        <span className="todo-progress-row__texts">
          {row.checked.map((todo) => todo.text).join(separator)}
        </span>
      ) : null}
      {records.length > 0 ? (
        <div className="todo-progress-row__records-list">
          <ProgressRecordList
            summary={t('todoProgress.records.period', {
              records: recordCount(i18n, records.length),
            })}
            records={records}
            today={today}
            {...recordActions}
          />
        </div>
      ) : null}
    </li>
  );
}
