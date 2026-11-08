import type {
  ProgressPeriodView,
  ProgressRecordView,
  TodoProgressPage,
  TodoProgressQuery,
} from '../../../../electron/src/todos/todoContracts';
import type { ProgressSpan } from '../../../todos/todoSelection';

/** A month's page has a row a day, a year's a row a month. */
export const PERIOD_OF_SPAN = {
  month: 'day',
  year: 'month',
} as const satisfies Readonly<Record<ProgressSpan, TodoProgressQuery['period']>>;

export const spanOf = (page: TodoProgressPage): ProgressSpan =>
  page.period === 'day' ? 'month' : 'year';

const DAY_MS = 86_400_000;
const WEEK_DAYS = 7;

/** A YYYY-MM-DD as midnight UTC, so the calendar arithmetic ignores the machine's time zone. */
const utcDay = (date: string) => new Date(`${date}T00:00:00Z`);

const shiftDays = (date: string, days: number) =>
  new Date(utcDay(date).getTime() + days * DAY_MS).toISOString().slice(0, 10);

/** The first day of the month (year) holding `day`, as the backend names a page. */
export const pageStartOf = (span: ProgressSpan, day: string) =>
  span === 'month' ? `${day.slice(0, 7)}-01` : `${day.slice(0, 4)}-01-01`;

/** The page `steps` months (years) away from the one starting on `pageStart`. */
export function shiftPage(span: ProgressSpan, pageStart: string, steps: number): string {
  const start = utcDay(pageStart);
  const year = start.getUTCFullYear() + (span === 'year' ? steps : 0);
  const month = start.getUTCMonth() + (span === 'month' ? steps : 0);
  return new Date(Date.UTC(year, month, 1)).toISOString().slice(0, 10);
}

/** The page holds today: its lead speaks of now, and no 「今月へ」 is needed. */
export const isCurrentPage = (page: TodoProgressPage) =>
  pageStartOf(spanOf(page), page.today) === page.page_start;

/** A day row belongs to the page's month, not to the weeks spilling into the months beside it. */
export const inPageMonth = (page: TodoProgressPage, row: ProgressPeriodView) =>
  row.start.slice(0, 7) === page.page_start.slice(0, 7);

/** A month's day rows as its Monday-to-Sunday weeks; the backend lays out whole weeks. */
export function weeksOf(rows: readonly ProgressPeriodView[]): ProgressPeriodView[][] {
  const weeks: ProgressPeriodView[][] = [];
  for (let index = 0; index < rows.length; index += WEEK_DAYS) {
    weeks.push(rows.slice(index, index + WEEK_DAYS));
  }
  return weeks;
}

/** The Monday-to-Sunday week holding `day`, from the day rows of its page. */
export function weekHolding(rows: readonly ProgressPeriodView[], day: string) {
  const monday = shiftDays(day, -((utcDay(day).getUTCDay() + 6) % WEEK_DAYS));
  const sunday = shiftDays(monday, WEEK_DAYS - 1);
  return rows.filter((row) => monday <= row.start && row.start <= sunday);
}

export type PeriodTotals = Pick<ProgressPeriodView, 'record_ids' | 'checked'>;

/**
 * What several rows recorded and checked together. The rows are the one source: a week's or a
 * page's numbers are their rows' records and TODOs, never a count of their own.
 */
export const totalsOf = (rows: readonly ProgressPeriodView[]): PeriodTotals => ({
  record_ids: [...new Set(rows.flatMap((row) => row.record_ids))],
  checked: rows.flatMap((row) => row.checked),
});

/** How much a cell holds, which its shade follows. */
export const activityOf = (totals: PeriodTotals) =>
  totals.record_ids.length + totals.checked.length;

/** One line of a cell: a project's records in one unit, added up. */
type RecordLine = { projectId: string | null; unit: string; quantity: number };

/** The records by project and unit, in the order they come; amounts in other units never add. */
export function recordLines(records: readonly ProgressRecordView[]): RecordLine[] {
  const lines = new Map<string, RecordLine>();
  for (const record of records) {
    const key = JSON.stringify([record.project_id, record.unit]);
    const line = lines.get(key);
    if (line) line.quantity += record.quantity;
    else
      lines.set(key, {
        projectId: record.project_id,
        unit: record.unit,
        quantity: record.quantity,
      });
  }
  return [...lines.values()];
}
