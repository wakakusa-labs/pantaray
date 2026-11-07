import { ChevronLeft, ChevronRight } from 'lucide-react';

import { useI18n } from '@/context/useI18n';

import type { TodoWeekPage } from '../../../electron/src/todos/todoContracts';
import { weekDay } from './todoDates';

import './todo.css';

/** The week in view, the buttons to the weeks on either side, and 「今週へ」 away from this week. */
export function WeekSwitcher({
  week,
  isCurrent,
  onPrevious,
  onNext,
  onCurrent,
}: {
  week: TodoWeekPage['week'];
  isCurrent: boolean;
  onPrevious: () => void;
  onNext: () => void;
  onCurrent: () => void;
}) {
  const { t, language } = useI18n();
  return (
    <div className="todo-week">
      <button
        type="button"
        className="history-filter-button todo-week__step"
        aria-label={t('todo.week.previous')}
        data-tooltip={t('todo.week.previous')}
        onClick={onPrevious}
      >
        <ChevronLeft size={14} aria-hidden="true" />
      </button>
      <span aria-live="polite">
        {t('todo.week.range', {
          start: weekDay(language, week.start),
          end: weekDay(language, week.end),
        })}
      </span>
      <button
        type="button"
        className="history-filter-button todo-week__step"
        aria-label={t('todo.week.next')}
        data-tooltip={t('todo.week.next')}
        onClick={onNext}
      >
        <ChevronRight size={14} aria-hidden="true" />
      </button>
      {isCurrent ? null : (
        <button type="button" className="history-filter-button" onClick={onCurrent}>
          {t('todo.week.current')}
        </button>
      )}
    </div>
  );
}
