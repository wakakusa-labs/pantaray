import { useLayoutEffect, useRef } from 'react';
import type { RefObject } from 'react';

const ACTIONABLE_CONTROL_SELECTOR = 'a, button, input, select, textarea:not([readonly])';
/**
 * A control that still works by pointer in the collapsed preview, like a read-only textarea: the
 * answer's copy button is in view there and changes nothing in the conversation.
 */
const COLLAPSED_CLICKABLE_ATTRIBUTE = 'data-collapsed-clickable';
const COLLAPSED_CONTROL_SELECTOR = `${ACTIONABLE_CONTROL_SELECTOR}, textarea[readonly]`;
const CONTROL_ATTRIBUTES = ['tabindex', 'aria-disabled', 'disabled', 'href'] as const;
type ControlAttribute = (typeof CONTROL_ATTRIBUTES)[number];

type ControlState = Record<ControlAttribute, string | null>;

function restoreAttribute(element: HTMLElement, name: string, value: string | null): void {
  if (value === null) element.removeAttribute(name);
  else element.setAttribute(name, value);
}

function readControlState(control: HTMLElement): ControlState {
  return {
    tabindex: control.getAttribute('tabindex'),
    'aria-disabled': control.getAttribute('aria-disabled'),
    disabled: control.getAttribute('disabled'),
    href: control.getAttribute('href'),
  };
}

export function useCollapsedFocusBoundary(
  rootRef: RefObject<HTMLDivElement>,
  isCollapsed: boolean,
  expandButtonRef: RefObject<HTMLButtonElement>
): void {
  const expandedControlStates = useRef(new Map<HTMLElement, ControlState>());

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    if (isCollapsed) {
      if (root.contains(document.activeElement)) {
        expandButtonRef.current?.focus();
        if (root.contains(document.activeElement)) (document.activeElement as HTMLElement).blur();
      }
      const disableControls = () => {
        root.querySelectorAll<HTMLElement>(COLLAPSED_CONTROL_SELECTOR).forEach((control) => {
          if (!expandedControlStates.current.has(control)) {
            expandedControlStates.current.set(control, readControlState(control));
          }
          control.setAttribute('tabindex', '-1');
          if (control instanceof HTMLTextAreaElement && control.readOnly) return;
          if (control.hasAttribute(COLLAPSED_CLICKABLE_ATTRIBUTE)) return;
          control.setAttribute('aria-disabled', 'true');
          if (control instanceof HTMLAnchorElement) control.removeAttribute('href');
          else control.setAttribute('disabled', '');
        });
      };
      const preventControlClick = (event: Event) => {
        const control =
          event.target instanceof Element
            ? event.target.closest(ACTIONABLE_CONTROL_SELECTOR)
            : null;
        if (!control || control.hasAttribute(COLLAPSED_CLICKABLE_ATTRIBUTE)) return;
        event.preventDefault();
        event.stopPropagation();
      };
      const preventSubmit = (event: Event) => {
        event.preventDefault();
        event.stopPropagation();
      };
      disableControls();
      const recordAttributeChanges = (records: MutationRecord[]) => {
        for (const record of records) {
          if (record.type !== 'attributes' || !(record.target instanceof HTMLElement)) continue;
          const state = expandedControlStates.current.get(record.target);
          if (!state) continue;
          const attribute = record.attributeName as ControlAttribute;
          state[attribute] = record.target.getAttribute(attribute);
        }
      };
      const observer = new MutationObserver((records) => {
        recordAttributeChanges(records);
        disableControls();
        observer.takeRecords();
      });
      observer.observe(root, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: [...CONTROL_ATTRIBUTES],
      });
      root.addEventListener('click', preventControlClick, true);
      root.addEventListener('submit', preventSubmit, true);
      return () => {
        recordAttributeChanges(observer.takeRecords());
        observer.disconnect();
        root.removeEventListener('click', preventControlClick, true);
        root.removeEventListener('submit', preventSubmit, true);
      };
    }

    expandedControlStates.current.forEach((state, control) => {
      CONTROL_ATTRIBUTES.forEach((attribute) => {
        restoreAttribute(control, attribute, state[attribute]);
      });
    });
    expandedControlStates.current.clear();
  }, [expandButtonRef, isCollapsed, rootRef]);
}
