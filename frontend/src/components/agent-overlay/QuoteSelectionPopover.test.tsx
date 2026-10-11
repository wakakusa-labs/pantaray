import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { QuoteCommentPopover } from './QuoteSelectionPopover';
import type { QuotableSelection } from './useQuotableSelection';

const POPOVER_HEIGHT = 90;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// Lays the popover out at the origin, as wide as it was set, as an untranslated element is.
function openAt(selection: QuotableSelection, onAdd = vi.fn()) {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement
  ) {
    const width = parseFloat(this.style.width) || 0;
    return {
      left: 0,
      top: 0,
      right: width,
      bottom: POPOVER_HEIGHT,
      width,
      height: POPOVER_HEIGHT,
    } as DOMRect;
  });
  render(
    <UiLanguageProvider initialLanguage="ja">
      <QuoteCommentPopover selection={selection} onAdd={onAdd} onCancel={vi.fn()} />
    </UiLanguageProvider>
  );
  const field = screen.getByRole('textbox', { name: '選んだ部分へのコメント' });
  return { field, popover: field.parentElement! };
}

const selected = { text: 'A案は安い。', endRight: 200, paneLeft: 0, paneRight: 1024 };

it('opens below the selection from its left edge, no wider than 420px', () => {
  const { popover } = openAt({ ...selected, top: 100, bottom: 120, left: 40 });
  expect(popover.style.width).toBe('420px');
  expect(popover.style.transform).toBe('translate(40px, 126px)');
});

it('flips above the selection without room below, and stays inside a narrow pane', () => {
  const { popover } = openAt({
    ...selected,
    top: 700,
    bottom: 720,
    left: 40,
    paneRight: 300,
  });
  // The pane's width less its margins, kept 8px from its left edge.
  expect(popover.style.width).toBe('284px');
  expect(popover.style.transform).toBe(`translate(8px, ${700 - 6 - POPOVER_HEIGHT}px)`);
});

it('shows what is quoted, and adds it with the optional comment from 追加', async () => {
  const onAdd = vi.fn();
  const { field } = openAt({ ...selected, top: 100, bottom: 120, left: 40 }, onAdd);
  expect(field).toHaveFocus();
  expect(field).toHaveAccessibleDescription('A案は安い。');
  expect(field).toHaveAttribute('placeholder', 'コメント（任意）');
  await userEvent.type(field, '高い');
  await userEvent.click(screen.getByRole('button', { name: '追加' }));
  expect(onAdd).toHaveBeenCalledWith('高い');
});
