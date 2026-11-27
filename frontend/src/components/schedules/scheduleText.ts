import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { MessageKey, UiLanguage } from '@/i18n/types';

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

export type ScheduleFieldMatch = {
  field: string;
  op: 'equals' | 'contains';
  values: readonly string[];
};

/**
 * When a schedule sends: once `at`, every calendar unit or duration (`offset` after each
 * unit's start), or each time a window starts to match every field of `match`. The approval
 * panel reads it from the tool's arguments; the screens that show a saved schedule word it
 * here too, so the user sees the text they approved.
 */
export type ScheduleTrigger =
  | { kind: 'at'; at: string }
  | { kind: 'every'; every: string; offset: string | null }
  | { kind: 'on'; match: readonly ScheduleFieldMatch[] };

const DURATION_UNIT_SECONDS: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400 };
const DURATION_UNITS = ['s', 'm', 'h', 'd'];

// The runtime writes every duration as one number and unit, such as 30m.
function durationSeconds(text: string | null): number {
  const match = text ? /^(\d+)([smhd])$/.exec(text) : null;
  return match ? Number(match[1]) * DURATION_UNIT_SECONDS[match[2]] : 0;
}

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * A moment on local wall time, as the schedule fires on it: 「11月30日(月) 9:00」,
 * "Mon, Nov 30, 9:00 AM", with the year only when it is not this one.
 */
export function formatInstant(text: string, language: UiLanguage): string {
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return new Intl.DateTimeFormat(getLocaleForUiLanguage(language), {
    year: date.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
    weekday: 'short',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}

// A calendar unit fires at its start on local time plus the offset: weeks start on
// Monday and months on the 1st.
function describeTime(every: string, offsetText: string | null, t: Translate): string {
  const offset = durationSeconds(offsetText);
  const days = Math.floor(offset / 86400);
  const time = `${Math.floor((offset % 86400) / 3600)}:${pad(Math.floor((offset % 3600) / 60))}`;
  switch (every) {
    case 'hour':
      return t('overlay.approvalRequired.hook.everyHour', { minute: pad(Math.floor(offset / 60)) });
    case 'day':
      return t('overlay.approvalRequired.hook.everyDay', { time });
    case 'week':
      return t('overlay.approvalRequired.hook.everyWeek', {
        weekday: t('overlay.approvalRequired.hook.weekdays').split(',')[days % 7] ?? '',
        time,
      });
    case 'month':
      return t('overlay.approvalRequired.hook.everyMonth', { day: String(days + 1), time });
    // A quarter's offset may run past its first month (up to 89 days), so only an
    // offset inside the shortest month reads as a day of that month.
    case 'quarter':
      return days < 28
        ? t('overlay.approvalRequired.hook.everyQuarter', { day: String(days + 1), time })
        : t('overlay.approvalRequired.hook.everyQuarterAfter', { days: String(days), time });
  }
  const match = /^(\d+)([smhd])$/.exec(every);
  if (!match) return every;
  return t('overlay.approvalRequired.hook.everyDuration', {
    count: match[1],
    unit:
      t('overlay.approvalRequired.hook.units').split(',')[DURATION_UNITS.indexOf(match[2])] ??
      match[2],
  });
}

// The fields an activity condition can name; the runtime refuses any other.
const MATCH_FIELD_KEYS: Record<string, MessageKey> = {
  app_name: 'overlay.approvalRequired.hook.field.appName',
  bundle_id: 'overlay.approvalRequired.hook.field.bundleId',
  window_title: 'overlay.approvalRequired.hook.field.windowTitle',
};

// The condition is worded from the stored match, never from the model's paraphrase:
// every field must match, and a field matches when any one of its values does.
function describeCondition(matches: readonly ScheduleFieldMatch[], t: Translate): string {
  const clauses = matches.map((match, index) =>
    t(
      `overlay.approvalRequired.hook.${match.op}${index === matches.length - 1 ? 'Last' : 'Middle'}`,
      {
        field: MATCH_FIELD_KEYS[match.field] ? t(MATCH_FIELD_KEYS[match.field]) : match.field,
        values: match.values
          .map((value) => t('overlay.approvalRequired.hook.quoted', { value }))
          .join(t('overlay.approvalRequired.hook.valueOr')),
      }
    )
  );
  return t('overlay.approvalRequired.hook.windowAppears', { conditions: clauses.join('') });
}

/** When a schedule sends: a time, or the window it waits for. */
export function describeTrigger(
  trigger: ScheduleTrigger,
  t: Translate,
  language: UiLanguage
): string {
  switch (trigger.kind) {
    case 'at':
      return t('overlay.approvalRequired.hook.once', { time: formatInstant(trigger.at, language) });
    case 'every':
      return describeTime(trigger.every, trigger.offset, t);
    case 'on':
      return describeCondition(trigger.match, t);
  }
}
