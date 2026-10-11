import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ChevronDown } from 'lucide-react';

export type FileOpenMenuItem = Readonly<{ label: string; onSelect: () => void }>;

type FileOpenMenuProps = {
  label: string;
  items: readonly FileOpenMenuItem[];
};

/**
 * A button that lists the ways to open a file. The menu follows the ARIA menu-button pattern:
 * opening it focuses the first item, arrow keys, Home and End move between items, and Escape
 * closes it and returns focus to the button.
 */
export function FileOpenMenu({ label, items }: FileOpenMenuProps) {
  const [isOpen, setIsOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
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
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []
    );
    const current = menuItems.indexOf(document.activeElement as HTMLButtonElement);
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
    <div className="task-file-menu" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="task-file-preview__open"
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={isOpen ? menuId : undefined}
        onClick={() => setIsOpen((open) => !open)}
      >
        {label}
        <ChevronDown size={13} strokeWidth={1.8} aria-hidden />
      </button>
      {isOpen ? (
        <div
          ref={menuRef}
          id={menuId}
          className="task-file-menu__list"
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
        >
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="task-file-menu__item"
              onClick={() => {
                close();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
