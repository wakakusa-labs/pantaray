import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { KeyboardEvent } from 'react';

import { useI18n } from '@/context/useI18n';

import { formatPage, formatWeekday } from './progress/progressText';
import { fullDay, monthGrid, shiftDays, shiftMonths } from './todoDates';

/** Days an arrow key moves the focus by: a day sideways, a week up or down. */
const ARROW_STEPS: Partial<Record<string, number>> = {
  ArrowLeft: -1,
  ArrowRight: 1,
  ArrowUp: -7,
  ArrowDown: 7,
};

/**
 * One month of the range picker, Monday first, with the range from `start` to `end` shaded. The
 * days are native buttons with one in the tab order; the arrow keys move the focus, which the
 * picker places, by day or week, into the next month when they leave this one.
 */
export function DateRangeMonth({
  month,
  today,
  start,
  due,
  end,
  focusDay,
  earliest,
  onMonth,
  onFocusDay,
  onPoint,
  onPick,
}: {
  /** Any day of the month in view. */
  month: string;
  today: string;
  start: string | null;
  due: string | null;
  /** The range's last day: the due day, or the day pointed at while the due is being picked. */
  end: string | null;
  /** The day in the tab order, when it is in view. */
  focusDay: string;
  /** Days before it cannot be picked: a due day before a start that stays. */
  earliest: string | null;
  onMonth: (month: string) => void;
  onFocusDay: (day: string) => void;
  onPoint: (day: string | null) => void;
  onPick: (day: string) => void;
}) {
  const i18n = useI18n();
  const { t, language } = i18n;
  const days = monthGrid(month);
  const first = shiftMonths(month, 0);
  const inMonth = (day: string) => day.slice(0, 7) === first.slice(0, 7);
  const tabbable = days.includes(focusDay) ? focusDay : first;

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, day: string) => {
    const step = ARROW_STEPS[event.key];
    if (step === undefined) return;
    event.preventDefault();
    onFocusDay(shiftDays(day, step));
  };
  const rangeOf = (day: string) => {
    if (start === null || end === null || start === end)
      return day === start || day === end ? 'single' : undefined;
    if (day === start) return 'start';
    if (day === end) return 'end';
    return start < day && day < end ? 'between' : undefined;
  };

  return (
    <div className="todo-date-picker__month">
      <div className="todo-date-picker__month-head">
        <button
          type="button"
          className="todo-date-picker__nav"
          aria-label={t('todoProgress.page.previousMonth')}
          data-tooltip={t('todoProgress.page.previousMonth')}
          onClick={() => onMonth(shiftMonths(month, -1))}
        >
          <ChevronLeft size={15} strokeWidth={1.8} aria-hidden="true" />
        </button>
        <span className="todo-date-picker__month-name">{formatPage(i18n, 'month', first)}</span>
        <button
          type="button"
          className="todo-date-picker__nav"
          aria-label={t('todoProgress.page.nextMonth')}
          data-tooltip={t('todoProgress.page.nextMonth')}
          onClick={() => onMonth(shiftMonths(month, 1))}
        >
          <ChevronRight size={15} strokeWidth={1.8} aria-hidden="true" />
        </button>
      </div>
      {/* Each day's name says its weekday, so the column heads are drawn only. */}
      <div className="todo-date-picker__days" aria-hidden="true">
        {days.slice(0, 7).map((day) => (
          <span key={day} className="todo-date-picker__weekday">
            {formatWeekday(language, day)}
          </span>
        ))}
      </div>
      <div
        className="todo-date-picker__days"
        role="group"
        aria-label={formatPage(i18n, 'month', first)}
        onMouseLeave={() => onPoint(null)}
      >
        {days.map((day) => {
          const blocked = earliest !== null && day < earliest;
          return (
            <button
              key={day}
              type="button"
              className="todo-date-picker__day"
              tabIndex={day === tabbable ? 0 : -1}
              aria-label={fullDay(language, day)}
              aria-current={day === today ? 'date' : undefined}
              aria-pressed={day === start || day === due ? true : undefined}
              aria-disabled={blocked ? true : undefined}
              data-range={rangeOf(day)}
              data-outside={inMonth(day) ? undefined : ''}
              onKeyDown={(event) => onKeyDown(event, day)}
              onMouseEnter={() => onPoint(day)}
              onFocus={() => onPoint(day)}
              onClick={() => {
                if (!blocked) onPick(day);
              }}
            >
              {Number(day.slice(8))}
            </button>
          );
        })}
      </div>
    </div>
  );
}
