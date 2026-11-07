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

/** "11月2日(月)": one day of a week's range, with its weekday. */
export const weekDay = (language: UiLanguage, date: string) =>
  new Intl.DateTimeFormat(getLocaleForUiLanguage(language), {
    month: 'short',
    day: 'numeric',
    weekday: 'short',
    timeZone: 'UTC',
  }).format(utcDay(date));
