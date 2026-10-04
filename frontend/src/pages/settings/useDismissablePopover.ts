import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

const FIRST_FIELD_SELECTOR =
  'input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface DismissablePopoverOptions {
  isOpen: boolean;
  triggerRef: RefObject<HTMLButtonElement>;
  onDismiss: () => void;
}

export function useDismissablePopover(options: DismissablePopoverOptions) {
  const { isOpen, onDismiss, triggerRef } = options;
  const popoverRef = useRef<HTMLDivElement>(null);
  // Callers often pass an inline callback. Reading it through a ref keeps the open effect
  // below from re-running, and moving focus back to the first field, on every re-render.
  const onDismissRef = useRef(onDismiss);
  useLayoutEffect(() => {
    onDismissRef.current = onDismiss;
  }, [onDismiss]);

  const dismiss = useCallback(() => {
    onDismissRef.current();
    triggerRef.current?.focus();
  }, [triggerRef]);

  useEffect(() => {
    if (!isOpen) return;

    const popover = popoverRef.current;
    const firstField = popover?.querySelector<HTMLElement>(FIRST_FIELD_SELECTOR);
    (firstField ?? popover)?.focus();

    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      dismiss();
    };
    const dismissOnOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (popover?.contains(target) || triggerRef.current?.contains(target)) return;
      onDismissRef.current();
    };

    document.addEventListener('keydown', dismissOnEscape);
    document.addEventListener('pointerdown', dismissOnOutsidePointerDown);
    return () => {
      document.removeEventListener('keydown', dismissOnEscape);
      document.removeEventListener('pointerdown', dismissOnOutsidePointerDown);
    };
  }, [dismiss, isOpen, triggerRef]);

  return { dismiss, popoverRef };
}
