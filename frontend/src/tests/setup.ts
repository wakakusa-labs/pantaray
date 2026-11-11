// Vitest setup
// - React Testing Library の matcher を追加する
import '@testing-library/jest-dom/vitest';

// happy-dom has no Popover API; the browser tests open and close popovers.
if (!('hidePopover' in HTMLElement.prototype)) {
  Object.defineProperty(HTMLElement.prototype, 'hidePopover', { value: () => undefined });
}
