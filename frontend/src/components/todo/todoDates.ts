import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { MessageKey, UiLanguage } from '@/i18n/types';

const DAY_MS = 86_400_000;

/** A YYYY-MM-DD as midnight UTC, so day counts and formatting ignore the machine's time zone. */
const utcDay = (date: string) => new Date(`${date}T00:00:00Z`);

/** Whole days from one YYYY-MM-DD to another; negative when `to` is earlier. */
export const daysFrom = (from: string, to: string) =>
  Math.round((utcDay(to).getTime() - utcDay(from).getTime()) / DAY_MS);

/** The YYYY-MM-DD `days` calendar days after `date`; negative goes back. */
export const shiftDays = (date: string, days: number) =>
  new Date(utcDay(date).getTime() + days * DAY_MS).toISOString().slice(0, 10);

/** The first day of the month `months` after the month holding `date`; negative goes back. */
export const shiftMonths = (date: string, months: number) => {
  const day = utcDay(date);
  return new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + months, 1))
    .toISOString()
    .slice(0, 10);
};

/**
 * The six Monday-to-Sunday weeks from the Monday on or before the 1st of `month`'s month: every
 * month fits, and a fixed height keeps a picker opened above its button from moving its header.
 */
export const monthGrid = (month: string) => {
  const first = shiftMonths(month, 0);
  const monday = shiftDays(first, -((utcDay(first).getUTCDay() + 6) % 7));
  return Array.from({ length: 42 }, (_, index) => shiftDays(monday, index));
};

/** "11/10": the month and day of a YYYY-MM-DD, as both languages write them. */
export const monthDay = (date: string) => {
  const [, month, day] = date.split('-');
  return `${Number(month)}/${Number(day)}`;
};

/** The month and day on this computer's calendar of a moment the backend recorded. */
export const momentMonthDay = (at: string) => {
  const moment = new Date(at);
  return `${moment.getMonth() + 1}/${moment.getDate()}`;
};

const format = (
  language: UiLanguage,
  date: string,
  options: Pick<Intl.DateTimeFormatOptions, 'year' | 'month' | 'day' | 'weekday'>
) =>
  new Intl.DateTimeFormat(getLocaleForUiLanguage(language), { ...options, timeZone: 'UTC' }).format(
    utcDay(date)
  );

/** "日 11/8", "Sun 11/8": a due day with its weekday, as the TODO rows and the add row show it. */
export const dueDay = (language: UiLanguage, date: string) =>
  `${format(language, date, { weekday: 'short' })} ${monthDay(date)}`;

/** 「2026年11月14日(土)」, "Sat, November 14, 2026": a day named in full, for a calendar's days. */
export const fullDay = (language: UiLanguage, date: string) =>
  format(language, date, { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' });

/** A TODO's start and due day, YYYY-MM-DD; null for none. */
export type TodoDates = { start: string | null; due: string | null };

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

/**
 * 「11/14 → 11/20」: the dates in a few characters, start first. A start of today, the add bar's
 * own, is left out (「→ 11/20」), as is a due day of none (「11/14 →」); both left out is empty.
 */
export function compactDates(t: Translate, today: string, { start, due }: TodoDates): string {
  const from = start === today ? '' : start === null ? t('todo.date.unset') : monthDay(start);
  if (due === null) return from === '' ? '' : `${from} →`;
  return from === '' ? `→ ${monthDay(due)}` : `${from} → ${monthDay(due)}`;
}

/** 「今日から、期限なし」: the dates in words, for the name of a button that shows them short. */
export const datesName = (
  t: Translate,
  language: UiLanguage,
  today: string,
  { start, due }: TodoDates
) =>
  t('todo.add.dates', {
    start:
      start === null
        ? t('todo.state.unscheduled')
        : start === today
          ? t('todo.add.startsToday')
          : t('todo.state.startsOn', { date: dueDay(language, start) }),
    due: due === null ? t('todo.add.noDue') : t('todo.add.due', { date: dueDay(language, due) }),
  });
