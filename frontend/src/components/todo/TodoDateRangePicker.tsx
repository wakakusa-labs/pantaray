import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { flushSync } from 'react-dom';

import { useI18n } from '@/context/useI18n';

import { DateRangeMonth } from './DateRangeMonth';
import { dueDay, monthGrid, type TodoDates } from './todoDates';

import './todo.css';
import './todoAddBar.css';

type End = 'start' | 'due';

/**
 * A button that opens a TODO's start and due day on one calendar, as a flight's outward and
 * return days are picked: a first click picks the start, a second the due day, and the days
 * between are shaded. A click before the start begins the range again from it, so a due day
 * before its start cannot be picked. With `startFixed` only the due day changes, on or after the
 * start. The header says which end the next click picks, and either end can be chosen there.
 *
 * The panel edits a draft. It is handed to `onChange` once, if it changed, as the panel closes:
 * on the second click, on 開始を未定にする or 期限なし, or on Escape or a click outside. It is a
 * native popover anchored to its button, so those close it and focus returns to the button.
 */
export function TodoDateRangePicker({
  triggerLabel,
  tooltip,
  triggerClassName,
  dates,
  today,
  opensOn,
  startFixed = false,
  disabled,
  onChange,
  children,
}: {
  /** The button's name: its text alone does not say both dates. */
  triggerLabel: string;
  /** Names the button in the app's tooltip, for a button drawn as an icon. */
  tooltip?: boolean;
  triggerClassName: string;
  dates: TodoDates;
  /** The backend's today, marked on the calendar. */
  today: string;
  /** The end the first click picks: the one the button shows. */
  opensOn: End;
  /** The start can no longer change: only the due day is picked. */
  startFixed?: boolean;
  /** A write on the TODO is waiting; dates chosen now would race it, so they are dropped. */
  disabled?: boolean;
  onChange: (dates: TodoDates) => void;
  children: ReactNode;
}) {
  const { t, language } = useI18n();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const firstEnd: End = startFixed ? 'due' : opensOn;
  const shownDay = (end: End) => (end === 'due' ? dates.due : dates.start) ?? today;
  const [draft, setDraft] = useState(dates);
  const [picking, setPicking] = useState<End>(firstEnd);
  const [month, setMonth] = useState(() => shownDay(firstEnd));
  const [focusDay, setFocusDay] = useState(() => shownDay(firstEnd));
  const [pointed, setPointed] = useState<string | null>(null);
  // A list has two pickers a row, so a closed panel draws nothing inside.
  const [open, setOpen] = useState(false);
  // The open panel's draft, read when it closes; null once it was handed over or while closed.
  const openDraftRef = useRef<TodoDates | null>(null);
  // Counts the moves of the focus asked for: on opening, and by the arrow keys.
  const [focusMoves, setFocusMoves] = useState(0);

  const edit = (next: TodoDates) => {
    openDraftRef.current = next;
    setDraft(next);
  };
  const handOver = () => {
    const next = openDraftRef.current;
    openDraftRef.current = null;
    if (next === null || disabled) return;
    if (next.start === dates.start && next.due === dates.due) return;
    // A panel let go by a click elsewhere closes in a browser event React does not render in
    // time for that click, so the new dates are rendered before it acts (the add bar's 追加).
    flushSync(() => onChange(next));
  };
  const close = (next: TodoDates) => {
    edit(next);
    handOver();
    panelRef.current?.hidePopover();
  };

  // React 18's types name no popover attributes, so they are set here once both elements
  // exist. The button as the popover's invoker anchors it and is exempt from its light dismiss.
  useLayoutEffect(() => {
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    panel.popover = 'auto';
    trigger.popoverTargetElement = panel;
    // A closing panel hands its draft over before the click that closed it acts: `beforetoggle`
    // fires within the light dismiss, while `toggle` comes later as a task of its own.
    const onBeforeToggle = (event: Event) => {
      if ((event as ToggleEvent).newState === 'closed') handOver();
    };
    // Opening waits for `toggle`: the panel is shown by then, so its day can take the focus.
    const onToggle = (event: Event) => {
      const open = (event as ToggleEvent).newState === 'open';
      // The tooltip steps aside while the panel it opened is in view.
      trigger.setAttribute('aria-expanded', String(open));
      setOpen(open);
      if (!open) return;
      openDraftRef.current = dates;
      setDraft(dates);
      setPicking(firstEnd);
      setMonth(shownDay(firstEnd));
      setFocusDay(shownDay(firstEnd));
      setPointed(null);
      setFocusMoves((count) => count + 1);
    };
    panel.addEventListener('beforetoggle', onBeforeToggle);
    panel.addEventListener('toggle', onToggle);
    return () => {
      panel.removeEventListener('beforetoggle', onBeforeToggle);
      panel.removeEventListener('toggle', onToggle);
    };
  });

  useLayoutEffect(() => {
    if (focusMoves > 0)
      panelRef.current?.querySelector<HTMLButtonElement>('[tabindex="0"]')?.focus();
  }, [focusMoves]);

  const pick = (day: string) => {
    if (picking === 'start') {
      // A due day before the new start no longer makes a range.
      edit({ start: day, due: draft.due !== null && draft.due < day ? null : draft.due });
      setPicking('due');
    } else if (draft.start !== null && day < draft.start) {
      edit({ start: day, due: null });
    } else {
      close({ ...draft, due: day });
    }
  };
  // While the due day is picked, the day pointed at shows the range it would make.
  const end =
    picking === 'due' && pointed !== null && draft.start !== null && pointed >= draft.start
      ? pointed
      : draft.due;

  const endButton = (which: End, value: string | null, none: string) => {
    const name = t(which === 'start' ? 'todo.date.start' : 'todo.date.due');
    const day = value === null ? none : dueDay(language, value);
    const content = (
      <>
        <span className="todo-date-picker__end-name">{name}</span>
        <span className="todo-date-picker__end-day">{day}</span>
      </>
    );
    if (which === 'start' && startFixed)
      return <span className="todo-date-picker__end">{content}</span>;
    return (
      <button
        type="button"
        className="todo-date-picker__end"
        aria-label={`${name} ${day}`}
        aria-pressed={picking === which}
        onClick={() => setPicking(which)}
      >
        {content}
      </button>
    );
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName}
        aria-label={triggerLabel}
        data-tooltip={tooltip ? triggerLabel : undefined}
        aria-haspopup="dialog"
      >
        {children}
      </button>
      <div
        ref={panelRef}
        className="todo-date-picker"
        role="dialog"
        aria-label={t('todo.date.range')}
      >
        {open ? (
          <>
            <div className="todo-date-picker__ends">
              {endButton('start', draft.start, t('todo.date.unset'))}
              <span className="todo-date-picker__arrow" aria-hidden="true">
                →
              </span>
              {endButton('due', draft.due, t('todo.date.none'))}
            </div>
            <DateRangeMonth
              month={month}
              today={today}
              start={draft.start}
              due={draft.due}
              end={end}
              focusDay={focusDay}
              earliest={startFixed ? draft.start : null}
              onMonth={setMonth}
              onFocusDay={(day) => {
                setFocusDay(day);
                // The grid shows a week or so of the months around; only a day past them turns it.
                if (!monthGrid(month).includes(day)) setMonth(day);
                setFocusMoves((count) => count + 1);
              }}
              onPoint={setPointed}
              onPick={pick}
            />
            <div className="todo-date-picker__choices">
              {startFixed ? null : (
                <button
                  type="button"
                  aria-pressed={draft.start === null}
                  onClick={() => close({ ...draft, start: null })}
                >
                  {t('todo.date.unsetStart')}
                </button>
              )}
              <button
                type="button"
                aria-pressed={draft.due === null}
                onClick={() => close({ ...draft, due: null })}
              >
                {t('todo.add.noDue')}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </>
  );
}
