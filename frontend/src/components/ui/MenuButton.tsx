import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';

import './menuButton.css';

export type MenuButtonItem = Readonly<{
  key: string;
  title: string;
  description?: string;
  /** Set when the item cannot be chosen; it is shown under the title and the press does nothing. */
  unavailableReason?: string | null;
  /** Set on a choice menu: the item is a `menuitemradio` with a check when true. */
  checked?: boolean;
  onSelect: () => void;
}>;

type MenuButtonProps = {
  /** The button's and the menu's accessible name; it must contain the visible text. */
  label: string;
  /** `value` shows the current choice; `icon` is a compact actions button such as •••. */
  variant?: 'value' | 'icon';
  /** Work started from the menu is still running: the button does not open until it ends. */
  busy?: boolean;
  items: readonly MenuButtonItem[];
  children: ReactNode;
};

const ITEM_SELECTOR = '[role="menuitem"], [role="menuitemradio"]';

/**
 * A button that opens a menu, following the ARIA menu-button pattern: opening it focuses the
 * checked item (or the first), arrow keys, Home and End move between items, Enter and Space
 * choose, and Escape closes it and returns focus to the button. An unavailable item stays
 * focusable so its reason can be read, but cannot be chosen.
 */
export function MenuButton({
  label,
  variant = 'value',
  busy = false,
  items,
  children,
}: MenuButtonProps) {
  const [isOpen, setIsOpen] = useState(false);
  const id = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const menu = menuRef.current;
    (
      menu?.querySelector<HTMLElement>('[aria-checked="true"]') ??
      menu?.querySelector<HTMLElement>(ITEM_SELECTOR)
    )?.focus();
    const closeOutside = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setIsOpen(false);
    };
    document.addEventListener('mousedown', closeOutside);
    return () => document.removeEventListener('mousedown', closeOutside);
  }, [isOpen]);

  const close = () => {
    setIsOpen(false);
    triggerRef.current?.focus();
  };

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const menuItems = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? []
    );
    const current = menuItems.indexOf(document.activeElement as HTMLElement);
    const next = {
      ArrowDown: (current + 1) % menuItems.length,
      ArrowUp: (current - 1 + menuItems.length) % menuItems.length,
      Home: 0,
      End: menuItems.length - 1,
    }[event.key];
    if (next !== undefined) {
      event.preventDefault();
      menuItems[next]?.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'Tab') {
      setIsOpen(false);
    }
  };

  return (
    <div className="menu-button" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`menu-button__trigger menu-button__trigger--${variant}`}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={isOpen ? id : undefined}
        aria-disabled={busy || undefined}
        aria-busy={busy || undefined}
        onClick={() => {
          if (!busy) setIsOpen((open) => !open);
        }}
        onKeyDown={(event) => {
          if (busy || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) return;
          event.preventDefault();
          setIsOpen(true);
        }}
      >
        {children}
      </button>
      {isOpen ? (
        <div
          ref={menuRef}
          id={id}
          className="menu-button__menu"
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
        >
          {items.map((item) => {
            const itemId = `${id}-${item.key}`;
            const unavailable = Boolean(item.unavailableReason);
            const described = [
              item.description ? `${itemId}-description` : null,
              unavailable ? `${itemId}-reason` : null,
            ].filter(Boolean);
            return (
              <button
                key={item.key}
                type="button"
                role={item.checked === undefined ? 'menuitem' : 'menuitemradio'}
                aria-checked={item.checked}
                aria-disabled={unavailable || undefined}
                aria-labelledby={`${itemId}-title`}
                aria-describedby={described.length > 0 ? described.join(' ') : undefined}
                tabIndex={-1}
                className="menu-button__item"
                onClick={() => {
                  if (unavailable) return;
                  close();
                  item.onSelect();
                }}
              >
                <span className="menu-button__check" aria-hidden="true">
                  {item.checked ? '✓' : ''}
                </span>
                <span id={`${itemId}-title`} className="menu-button__title">
                  {item.title}
                </span>
                {item.description ? (
                  <span id={`${itemId}-description`} className="menu-button__description">
                    {item.description}
                  </span>
                ) : null}
                {unavailable ? (
                  <span id={`${itemId}-reason`} className="menu-button__reason">
                    {item.unavailableReason}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
