import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { UiLanguage } from '@/i18n/types';

const DAY_MS = 86_400_000;

/** A YYYY-MM-DD as midnight UTC, so day counts and formatting ignore the machine's time zone. */
const utcDay = (date: string) => new Date(`${date}T00:00:00Z`);

/** Whole days from one YYYY-MM-DD to another; negative when `to` is earlier. */
export const daysFrom = (from: string, to: string) =>
  Math.round((utcDay(to).getTime() - utcDay(from).getTime()) / DAY_MS);

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
  options: Pick<Intl.DateTimeFormatOptions, 'month' | 'day' | 'weekday'>
) =>
  new Intl.DateTimeFormat(getLocaleForUiLanguage(language), { ...options, timeZone: 'UTC' }).format(
    utcDay(date)
  );

/** "11月2日" and "8日", or "Nov 2" and "8": a week's two ends, its month named once when it is one. */
export const weekEnds = (language: UiLanguage, start: string, end: string) => ({
  start: format(language, start, { month: 'short', day: 'numeric' }),
  end: format(
    language,
    end,
    start.slice(0, 7) === end.slice(0, 7) ? { day: 'numeric' } : { month: 'short', day: 'numeric' }
  ),
});

/** "日 11/8", "Sun 11/8": a due day with its weekday, as the TODO rows and the add row show it. */
export const dueDay = (language: UiLanguage, date: string) =>
  `${format(language, date, { weekday: 'short' })} ${monthDay(date)}`;
