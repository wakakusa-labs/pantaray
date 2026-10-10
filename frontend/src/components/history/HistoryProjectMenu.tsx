import { MoreHorizontal } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';

/** Keeps the menu clear of the window edges. */
const VIEWPORT_MARGIN_PX = 8;
const ANCHOR_GAP_PX = 4;

export type HistoryProjectMenuItem = {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
};

/**
 * A row's 「…」 menu. The project list scrolls and clips, so the menu is portalled to the body
 * and placed against its button; it closes on scroll or resize rather than following it.
 */
export function HistoryProjectMenu({
  label,
  buttonId,
  items,
  disabled,
}: {
  label: string;
  buttonId: string;
  items: HistoryProjectMenuItem[];
  /** While another workspace change runs; the changes here would be refused. */
  disabled: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const close = (returnFocus: boolean) => {
    setIsOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  };

  useLayoutEffect(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!isOpen || !trigger || !menu) return;
    const anchor = trigger.getBoundingClientRect();
    const below = anchor.bottom + ANCHOR_GAP_PX;
    setPosition({
      top:
        below + menu.offsetHeight <= window.innerHeight - VIEWPORT_MARGIN_PX
          ? below
          : Math.max(VIEWPORT_MARGIN_PX, anchor.top - ANCHOR_GAP_PX - menu.offsetHeight),
      left: Math.max(VIEWPORT_MARGIN_PX, anchor.right - menu.offsetWidth),
    });
    menu.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const closeOnPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setIsOpen(false);
    };
    const closeOnMove = (event: Event) => {
      if (!menuRef.current?.contains(event.target as Node)) setIsOpen(false);
    };
    document.addEventListener('pointerdown', closeOnPointerDown);
    window.addEventListener('scroll', closeOnMove, true);
    window.addEventListener('resize', closeOnMove);
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown);
      window.removeEventListener('scroll', closeOnMove, true);
      window.removeEventListener('resize', closeOnMove);
    };
  }, [isOpen]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const menuItems = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []
    );
    const current = menuItems.indexOf(document.activeElement as HTMLButtonElement);
    const focusAt = (index: number) =>
      menuItems[(index + menuItems.length) % menuItems.length]?.focus();
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'Tab') {
      close(false);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      focusAt(current + (event.key === 'ArrowDown' ? 1 : -1));
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      focusAt(event.key === 'Home' ? 0 : -1);
    }
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={buttonId}
        className="history-item-delete history-project__menu-button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        disabled={disabled}
        aria-expanded={isOpen}
        aria-controls={isOpen ? menuId : undefined}
        onClick={() => setIsOpen((open) => !open)}
      >
        <MoreHorizontal size={15} aria-hidden="true" />
      </button>
      {isOpen
        ? createPortal(
            <div
              ref={menuRef}
              id={menuId}
              role="menu"
              aria-label={label}
              className="history-project-menu"
              style={position}
              onKeyDown={handleKeyDown}
            >
              {items.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  role="menuitem"
                  className="history-project-menu__item"
                  disabled={item.disabled}
                  onClick={() => {
                    // Focus returns to the button; an action that opens a field or dialog
                    // takes it from there.
                    close(true);
                    item.onSelect();
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>,
            document.body
          )
        : null}
    </>
  );
}
