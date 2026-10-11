import { expect } from 'vitest';

/**
 * Every icon-only button under `root` names itself in the app-drawn tooltip (iconTooltip.css)
 * and carries no native `title`, which would show a second, late tooltip. Returns the buttons.
 */
export function expectIconButtonTooltips(root: ParentNode): HTMLButtonElement[] {
  const iconOnly = Array.from(root.querySelectorAll('button')).filter(
    (button) => button.querySelector('svg') !== null && button.textContent?.trim() === ''
  );
  expect(iconOnly.length).toBeGreaterThan(0);
  for (const button of iconOnly) {
    expect(button).not.toHaveAttribute('title');
    expect(button.dataset.tooltip, button.getAttribute('aria-label') ?? '').toBeTruthy();
  }
  return iconOnly;
}

/** The tooltip says what the accessible name says, so sighted and screen reader users agree. */
export function expectTooltipIsName(button: HTMLElement) {
  expect(button.dataset.tooltip).toBe(button.getAttribute('aria-label'));
}
