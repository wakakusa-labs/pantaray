import { Check, ChevronDown } from 'lucide-react';
import { useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';

import './quickTodo.css';

export type QuickTodoChoice = { id: string | null; name: string };

const MOVES: Readonly<Record<string, (current: number, last: number) => number>> = {
  ArrowDown: (current, last) => (current >= last ? 0 : current + 1),
  ArrowUp: (current, last) => (current <= 0 ? last : current - 1),
  Home: () => 0,
  End: (_current, last) => last,
};

/**
 * A button naming what is picked, which opens the choices under it as a native popover: Escape or
 * a click elsewhere closes it and the focus goes back to the button. The arrow keys, Home and End
 * move through the choices; Enter or Space picks one.
 */
export function QuickTodoPicker({
  label,
  mark,
  value,
  choices,
  selected,
  onSelect,
}: {
  /** What is picked, such as 組織. */
  label: string;
  /** Drawn before the name picked. */
  mark: ReactNode;
  /** The name picked. */
  value: string;
  choices: readonly QuickTodoChoice[];
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const [open, setOpen] = useState(false);

  // React 18's types name no popover attributes, so they are set once both elements exist. The
  // button as the popover's invoker anchors it and is exempt from its light dismiss.
  useLayoutEffect(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    menu.popover = 'auto';
    trigger.popoverTargetElement = menu;
    const onToggle = (event: Event) => {
      const opened = (event as ToggleEvent).newState === 'open';
      setOpen(opened);
      if (opened) menu.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    };
    menu.addEventListener('toggle', onToggle);
    return () => menu.removeEventListener('toggle', onToggle);
  }, []);

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = MOVES[event.key];
    if (!step) return;
    event.preventDefault();
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button'));
    const current = items.findIndex((item) => item === document.activeElement);
    items[step(current, items.length - 1)].focus();
  };

  const pick = (id: string | null) => {
    onSelect(id);
    menuRef.current?.hidePopover();
    triggerRef.current?.focus();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="quick-todo__picker"
        aria-label={`${label}: ${value}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
      >
        {mark}
        <span className="quick-todo__name">{value}</span>
        <ChevronDown className="quick-todo__caret" strokeWidth={2} aria-hidden="true" />
      </button>
      <div
        ref={menuRef}
        id={menuId}
        className="quick-todo__menu"
        role="menu"
        aria-label={label}
        onKeyDown={move}
      >
        {choices.map((choice) => (
          <button
            key={choice.id ?? ''}
            type="button"
            role="menuitemradio"
            aria-checked={choice.id === selected}
            onClick={() => pick(choice.id)}
          >
            <span className="quick-todo__name">{choice.name}</span>
            {choice.id === selected ? <Check size={14} strokeWidth={2} aria-hidden="true" /> : null}
          </button>
        ))}
      </div>
    </>
  );
}
