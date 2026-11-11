import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

import { useI18n } from '@/context/useI18n';

import { nextMonday, shiftDays } from './todoDates';

import './todo.css';

/**
 * A button that opens a small date picker under it: 今日, 明日 and 来週, the native date field,
 * and, when `clearLabel` is given, a choice that takes the date away. It is a native popover
 * anchored to its button, so Escape and a click outside close it and focus returns to the
 * button. A choice closes it.
 */
export function TodoDatePicker({
  label,
  triggerLabel,
  triggerClassName,
  value,
  today,
  clearLabel,
  disabled,
  onChoose,
  children,
}: {
  /** The picker's name: what the date is. */
  label: string;
  /** The button's name, when its text alone does not say which date it changes. */
  triggerLabel?: string;
  triggerClassName: string;
  /** The date chosen now, YYYY-MM-DD; null for none. */
  value: string | null;
  /** The backend's today, which 今日, 明日 and 来週 count from. */
  today: string;
  clearLabel?: string;
  /** A write on the TODO is waiting; a choice now would race it, so it is dropped. */
  disabled?: boolean;
  onChoose: (date: string | null) => void;
  children: ReactNode;
}) {
  const { t } = useI18n();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState(value ?? '');
  // Typing into the date field passes through dates on the way (the year 0002 on the way to
  // 2026), so a typed date waits for Enter; a date picked from the field's calendar is taken.
  const typedRef = useRef(false);

  // React 18's types name no popover attributes, so they are set here once both elements
  // exist. The button as the popover's invoker anchors it and is exempt from its light dismiss.
  useLayoutEffect(() => {
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    panel.popover = 'auto';
    trigger.popoverTargetElement = panel;
    const onToggle = (event: Event) => {
      if (!(event instanceof ToggleEvent) || event.newState !== 'open') return;
      setDraft(value ?? '');
      typedRef.current = false;
      panel.querySelector('button')?.focus();
    };
    panel.addEventListener('toggle', onToggle);
    return () => panel.removeEventListener('toggle', onToggle);
  }, [value]);

  const choose = (date: string | null) => {
    panelRef.current?.hidePopover();
    if (!disabled && date !== value) onChoose(date);
  };
  const quick: [string, string][] = [
    [t('todo.date.today'), today],
    [t('todo.date.tomorrow'), shiftDays(today, 1)],
    [t('todo.date.nextWeek'), nextMonday(today)],
  ];
  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName}
        aria-label={triggerLabel}
        aria-haspopup="dialog"
      >
        {children}
      </button>
      <div ref={panelRef} className="todo-date-picker" role="dialog" aria-label={label}>
        <div className="todo-date-picker__quick">
          {quick.map(([name, date]) => (
            <button
              key={name}
              type="button"
              aria-pressed={date === value}
              onClick={() => choose(date)}
            >
              {name}
            </button>
          ))}
        </div>
        <input
          type="date"
          className="todo-date-picker__field"
          aria-label={t('todo.date.pick')}
          value={draft}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') {
              typedRef.current = true;
              return;
            }
            event.preventDefault();
            if (draft) choose(draft);
          }}
          onChange={(event) => {
            const date = event.target.value;
            setDraft(date);
            if (date && !typedRef.current) choose(date);
          }}
        />
        {clearLabel ? (
          <button type="button" className="todo-date-picker__clear" onClick={() => choose(null)}>
            {clearLabel}
          </button>
        ) : null}
      </div>
    </>
  );
}
