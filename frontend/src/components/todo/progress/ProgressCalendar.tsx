import type { CSSProperties } from 'react';

import { useI18n } from '@/context/useI18n';

import type {
  ProgressPeriodView,
  ProgressRecordView,
  TodoProgressPage,
} from '../../../../electron/src/todos/todoContracts';
import {
  activityOf,
  inPageMonth,
  recordLines,
  totalsOf,
  weeksOf,
  type PeriodTotals,
} from './progressPages';
import {
  formatDay,
  formatMonth,
  formatMonthDay,
  formatPage,
  formatQuantity,
  formatWeekday,
  projectLabel,
  recordCount,
  recordsFor,
  todoCount,
  type ProjectNameOf,
} from './progressText';

import './progress.css';

/** A Monday-to-Sunday week, for the weekday names over the columns. */
const A_WEEK = [
  '2026-11-02',
  '2026-11-03',
  '2026-11-04',
  '2026-11-05',
  '2026-11-06',
  '2026-11-07',
  '2026-11-08',
];

type CalendarProps = {
  page: TodoProgressPage;
  recordsById: ReadonlyMap<string, ProgressRecordView>;
  /** Names each line's project; null when the page holds one project's records only. */
  projectName: ProjectNameOf | null;
};

/** A cell's shade follows how much it holds against the fullest cell on the page. */
const shade = (totals: PeriodTotals, most: number): CSSProperties =>
  ({ '--todo-progress-shade': most === 0 ? 0 : activityOf(totals) / most }) as CSSProperties;

/** A cell's records by project and unit, up to `maxLines`, then how many lines are left. */
function RecordLines({
  recordIds,
  maxLines,
  recordsById,
  projectName,
}: {
  recordIds: readonly string[];
  maxLines: number;
} & Omit<CalendarProps, 'page'>) {
  const i18n = useI18n();
  const lines = recordLines(recordsFor(recordIds, recordsById));
  return (
    <>
      {lines.slice(0, maxLines).map((line) => {
        const text = [
          projectName ? projectLabel(i18n, projectName, line.projectId) : null,
          formatQuantity(i18n, line.quantity, line.unit),
        ]
          .filter((part): part is string => part !== null)
          .join(' ');
        return (
          <span key={`${line.projectId}:${line.unit}`} className="todo-progress-cell__line">
            <span className="todo-progress-dot" />
            {/* A narrow cell cuts a long line short; the pointer still reads all of it. */}
            <span title={text}>{text}</span>
          </span>
        );
      })}
      {lines.length > maxLines ? (
        <span className="todo-progress-cell__line todo-progress-cell__line--muted">
          {i18n.t('todoProgress.calendar.more', { count: lines.length - maxLines })}
        </span>
      ) : null}
    </>
  );
}

/** 「✓ 2」 under a cell's records. */
function CheckedLine({ count }: { count: number }) {
  const { t } = useI18n();
  return count === 0 ? null : (
    <span className="todo-progress-cell__line todo-progress-cell__line--muted">
      {t('todoProgress.calendar.checked', { count })}
    </span>
  );
}

const DAY_LINES = 2;
const MONTH_LINES = 3;

/**
 * The month as a wall calendar, Monday first, with each week's totals at its end. A day up to
 * today opens its records; a day ahead holds nothing yet.
 */
export function MonthCalendar({
  page,
  selectedDay,
  onSelectDay,
  ...lineProps
}: CalendarProps & {
  selectedDay: string | null;
  onSelectDay: (day: string) => void;
}) {
  const i18n = useI18n();
  const { t, language } = i18n;
  const past = page.rows.filter((row) => row.start <= page.today);
  const most = Math.max(0, ...past.map(activityOf));
  const dayCell = (row: ProgressPeriodView) => {
    const day = row.start;
    const otherMonth = !inPageMonth(page, row);
    const number =
      otherMonth && day.endsWith('-01') ? formatMonthDay(i18n, day) : Number(day.slice(8));
    const className = [
      'todo-progress-cell',
      otherMonth ? 'todo-progress-cell--other-month' : '',
      day === page.today ? 'todo-progress-cell--today' : '',
    ]
      .filter(Boolean)
      .join(' ');
    if (day > page.today) {
      return (
        <div key={day} className={`${className} todo-progress-cell--ahead`}>
          <span className="todo-progress-cell__date">{number}</span>
        </div>
      );
    }
    return (
      <button
        key={day}
        type="button"
        className={className}
        style={shade(row, most)}
        aria-label={t('todoProgress.calendar.cell', {
          date: formatDay(i18n, day),
          records: recordCount(i18n, row.record_ids.length),
          todos: todoCount(i18n, row.checked.length),
        })}
        aria-pressed={day === selectedDay}
        aria-current={day === page.today ? 'date' : undefined}
        onClick={() => onSelectDay(day)}
      >
        <span className="todo-progress-cell__date">{number}</span>
        <RecordLines recordIds={row.record_ids} maxLines={DAY_LINES} {...lineProps} />
        <CheckedLine count={row.checked.length} />
      </button>
    );
  };
  return (
    <div
      className="todo-progress-calendar"
      role="group"
      aria-label={formatPage(i18n, 'month', page.page_start)}
    >
      {A_WEEK.map((day) => (
        <div key={day} className="todo-progress-calendar__weekday" aria-hidden="true">
          {formatWeekday(language, day)}
        </div>
      ))}
      <div
        className="todo-progress-calendar__weekday todo-progress-calendar__weekday--total"
        aria-hidden="true"
      >
        {t('todoProgress.calendar.weekTotal')}
      </div>
      {weeksOf(page.rows).map((week) => {
        const begun = week[0].start <= page.today;
        const holdsToday = begun && page.today <= week[week.length - 1].start;
        const totals = totalsOf(week);
        return [
          ...week.map(dayCell),
          <div
            key={`${week[0].start}:total`}
            className={
              holdsToday ? 'todo-progress-week todo-progress-week--now' : 'todo-progress-week'
            }
          >
            {begun ? (
              <>
                <RecordLines recordIds={totals.record_ids} maxLines={DAY_LINES} {...lineProps} />
                {totals.checked.length > 0 ? (
                  <small>
                    {t('todoProgress.calendar.weekChecked', { count: totals.checked.length })}
                  </small>
                ) : null}
              </>
            ) : null}
          </div>,
        ];
      })}
    </div>
  );
}

/** The year as its twelve months; a month opens as its calendar. */
export function YearCalendar({
  page,
  onSelectMonth,
  ...lineProps
}: CalendarProps & { onSelectMonth: (monthStart: string) => void }) {
  const i18n = useI18n();
  const { t } = i18n;
  const most = Math.max(0, ...page.rows.map(activityOf));
  return (
    <div
      className="todo-progress-calendar todo-progress-calendar--year"
      role="group"
      aria-label={formatPage(i18n, 'year', page.page_start)}
    >
      {page.rows.map((row) => {
        const begun = row.start <= page.today;
        const now = begun && page.today <= row.end;
        return (
          <button
            key={row.start}
            type="button"
            className={now ? 'todo-progress-cell todo-progress-cell--now' : 'todo-progress-cell'}
            style={shade(row, most)}
            aria-label={t('todoProgress.calendar.cell', {
              date: formatPage(i18n, 'month', row.start),
              records: recordCount(i18n, row.record_ids.length),
              todos: todoCount(i18n, row.checked.length),
            })}
            onClick={() => onSelectMonth(row.start)}
          >
            <span className="todo-progress-cell__date">{formatMonth(i18n, row.start)}</span>
            {begun ? (
              <>
                <RecordLines recordIds={row.record_ids} maxLines={MONTH_LINES} {...lineProps} />
                <CheckedLine count={row.checked.length} />
              </>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
