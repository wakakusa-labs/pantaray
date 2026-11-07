import type { UiLanguageContextValue } from '@/context/uiLanguageContextShared';
import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { UiLanguage } from '@/i18n/types';

import type {
  GoalView,
  ProgressPace,
  ProgressRecordView,
  TodoProgressPage,
} from '../../../../electron/src/todos/todoContracts';

/** The words and number formats of the UI language. */
export type ProgressI18n = Pick<UiLanguageContextValue, 't' | 'language'>;

/** The project's name as the user wrote it; the page that mounts the view knows the projects. */
export type ProjectNameOf = (projectId: string) => string;

/** A recorded share below this reads as one significant digit, so a real step never reads 0%. */
const WHOLE_PERCENT = 1;
/** Rounding is downward, and float error must not push 29% (0.29 × 100 = 28.999…) to 28%. */
const FLOAT_SLACK = 1e-9;
/** A projected day is told as the part of its month: days 1–10, 11–20, then the rest. */
const EARLY_MONTH_LAST_DAY = 10;
const MID_MONTH_LAST_DAY = 20;

/** Below this, two decimals would round a positive quantity to 0. */
const SMALLEST_TWO_DECIMALS = 0.01;
/** A quantity under 0.01 keeps this many significant digits (0.001, 0.0025). */
const SMALL_SIGNIFICANT_DIGITS = 2;

/**
 * Records are held to positive quantities: two decimals keep 0.5 or 1.25 as written, and a
 * smaller one keeps its significant digits, so a positive amount never reads as 0.
 */
export const formatNumber = (language: UiLanguage, value: number): string =>
  new Intl.NumberFormat(
    getLocaleForUiLanguage(language),
    value > 0 && value < SMALLEST_TWO_DECIMALS
      ? { maximumSignificantDigits: SMALL_SIGNIFICANT_DIGITS }
      : { maximumFractionDigits: 2 }
  ).format(value);

/** A pace of large amounts a day is told in whole units; a small one as any quantity. */
const formatRate = (language: UiLanguage, value: number): string =>
  value >= 10
    ? new Intl.NumberFormat(getLocaleForUiLanguage(language), { maximumFractionDigits: 0 }).format(
        value
      )
    : formatNumber(language, value);

export const formatQuantity = ({ t, language }: ProgressI18n, value: number, unit: string) =>
  t('todoProgress.quantity', { number: formatNumber(language, value), unit });

/**
 * The share as a percent, rounded down: 99.6% is not yet there, and a share above 100% stays
 * above. Below 1% it keeps its first significant digit (0.03%).
 */
export function percentValue(share: number): number {
  const percent = share * 100;
  if (percent <= 0) return 0;
  if (percent >= WHOLE_PERCENT) return Math.floor(percent + FLOAT_SLACK);
  const scale = 10 ** Math.ceil(-Math.log10(percent));
  return Math.floor(percent * scale + FLOAT_SLACK) / scale;
}

/** `percentValue` has already rounded, so the format adds no rounding of its own. */
export const formatPercent = ({ t, language }: ProgressI18n, share: number) =>
  t('todoProgress.percent', {
    number: new Intl.NumberFormat(getLocaleForUiLanguage(language), {
      maximumFractionDigits: 20,
    }).format(percentValue(share)),
  });

export const recordCount = ({ t }: ProgressI18n, count: number) =>
  t(count === 1 ? 'todoProgress.count.records.one' : 'todoProgress.count.records.other', {
    count,
  });

export const todoCount = ({ t }: ProgressI18n, count: number) =>
  t(count === 1 ? 'todoProgress.count.todos.one' : 'todoProgress.count.todos.other', { count });

/** A YYYY-MM-DD as midnight UTC, so formatting ignores the machine's time zone. */
const utcDay = (date: string) => new Date(`${date}T00:00:00Z`);

const dateParts = (date: string) => {
  const [year, month, day] = date.split('-').map(Number);
  return { year, month, day };
};

/** The local calendar date of a moment the backend recorded. */
export function momentLocalDate(at: string): string {
  const moment = new Date(at);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`;
}

/** 「8/31」 in today's year, 「2027/8/31」 in another. */
export function formatDate({ t }: ProgressI18n, today: string, date: string): string {
  const parts = dateParts(date);
  return parts.year === dateParts(today).year
    ? t('todoProgress.shortDate', parts)
    : t('todoProgress.fullDate', parts);
}

const monthName = (language: UiLanguage, date: string) =>
  new Intl.DateTimeFormat(getLocaleForUiLanguage(language), {
    month: 'long',
    timeZone: 'UTC',
  }).format(utcDay(date));

/** 「11 月」 / "November". */
export const formatMonth = ({ t, language }: ProgressI18n, date: string) =>
  t('todoProgress.month', {
    month: dateParts(date).month,
    monthName: monthName(language, date),
  });

export const formatWeekday = (language: UiLanguage, date: string) =>
  new Intl.DateTimeFormat(getLocaleForUiLanguage(language), {
    weekday: 'short',
    timeZone: 'UTC',
  }).format(utcDay(date));

/** 「2027 年 6 月のはじめ」: a projection is not precise to the day. */
export function formatProjectedDay(i18n: ProgressI18n, date: string): string {
  const { t, language } = i18n;
  const { year, month, day } = dateParts(date);
  const yearMonth = t('todoProgress.yearMonth', {
    year,
    month,
    monthName: monthName(language, date),
  });
  const part =
    day <= EARLY_MONTH_LAST_DAY
      ? 'todoProgress.when.early'
      : day <= MID_MONTH_LAST_DAY
        ? 'todoProgress.when.mid'
        : 'todoProgress.when.late';
  return t(part, { month: yearMonth });
}

/** 「売上 300,000,000 円」. */
export const goalName = (i18n: ProgressI18n, goal: GoalView) =>
  i18n.t('todoProgress.goal.name', {
    text: goal.text,
    target: formatQuantity(i18n, goal.target, goal.unit),
  });

/** Whether, and when, the pace reaches the target; no date when the records cannot say. */
export function paceOutcome(i18n: ProgressI18n, pace: ProgressPace): string {
  const { t } = i18n;
  switch (pace.kind) {
    case 'reached':
      return t('todoProgress.pace.reached');
    case 'too_few_records':
      return t('todoProgress.pace.tooFew');
    case 'projected':
      return t('todoProgress.pace.projected', {
        when: formatProjectedDay(i18n, pace.projected_on),
      });
    case 'beyond_horizon':
      return t('todoProgress.pace.beyondHorizon');
  }
}

/** The pace itself and the records it comes from; null when there is no pace to tell. */
export function paceRate(
  i18n: ProgressI18n,
  today: string,
  pace: ProgressPace,
  unit: string
): string | null {
  if (pace.kind !== 'projected' && pace.kind !== 'beyond_horizon') return null;
  return i18n.t('todoProgress.pace.rate', {
    start: formatDate(i18n, today, pace.window_start),
    records: recordCount(i18n, pace.record_ids.length),
    perDay: i18n.t('todoProgress.quantity', {
      number: formatRate(i18n.language, pace.per_day),
      unit,
    }),
  });
}

/** Sentences side by side, as the language writes them. */
export const joinSentences = ({ t }: ProgressI18n, sentences: readonly string[]) =>
  sentences.join(t('todoProgress.sentenceSeparator'));

/** The name of a project, or of the TODOs in none. */
export const projectLabel = (
  { t }: ProgressI18n,
  projectName: ProjectNameOf,
  projectId: string | null
) => (projectId === null ? t('todoProgress.noProject') : projectName(projectId));

/** The records behind ids the page names; every id it names comes with its record. */
export function recordsFor(
  ids: readonly string[],
  recordsById: ReadonlyMap<string, ProgressRecordView>
): ProgressRecordView[] {
  return ids.map((id) => {
    const record = recordsById.get(id);
    // A number whose records cannot be shown is not traceable: fail loudly, never drop one.
    if (!record) throw new Error(`The progress page names a record it does not carry: ${id}`);
    return record;
  });
}

/** How many of the records each project has, in the order the records come. */
export function countByProject(records: readonly ProgressRecordView[]) {
  const counts = new Map<string | null, number>();
  for (const record of records) {
    counts.set(record.project_id, (counts.get(record.project_id) ?? 0) + 1);
  }
  return [...counts].map(([projectId, count]) => ({ projectId, count }));
}

/**
 * The sentence above the view: what the newest period recorded and checked, and, when the page
 * reports a single goal, where that goal stands. With several goals each speaks in its own block.
 */
export function leadSentence(
  i18n: ProgressI18n,
  page: TodoProgressPage,
  recordsById: ReadonlyMap<string, ProgressRecordView>,
  projectName: ProjectNameOf
): string {
  const { t } = i18n;
  const sentences: string[] = [];
  const [current] = page.rows;
  if (current) {
    const period =
      page.period === 'day'
        ? t('todoProgress.lead.day', { date: formatDate(i18n, page.today, current.start) })
        : t(page.period === 'week' ? 'todoProgress.lead.week' : 'todoProgress.lead.month');
    const counts = countByProject(recordsFor(current.record_ids, recordsById));
    sentences.push(
      counts.length === 0
        ? t('todoProgress.lead.noRecords', { period })
        : t('todoProgress.lead.records', {
            period,
            items: counts
              .map(({ projectId, count }) =>
                t('todoProgress.lead.recordsItem', {
                  project: projectLabel(i18n, projectName, projectId),
                  records: recordCount(i18n, count),
                })
              )
              .join(t('todoProgress.listSeparator')),
          })
    );
    if (current.checked.length > 0) {
      sentences.push(
        t('todoProgress.lead.checked', { todos: todoCount(i18n, current.checked.length) })
      );
    }
  }
  const [only] = page.goals;
  if (page.goals.length === 1 && only.kind === 'measured') {
    sentences.push(
      t('todoProgress.goal.toward', {
        goal: goalName(i18n, only.goal),
        share: formatPercent(i18n, only.share),
      }),
      paceOutcome(i18n, only.pace)
    );
  }
  if (page.goals.length === 1 && only.kind === 'no_records') {
    sentences.push(t('todoProgress.goal.towardNoRecords', { goal: goalName(i18n, only.goal) }));
  }
  return joinSentences(i18n, sentences);
}
