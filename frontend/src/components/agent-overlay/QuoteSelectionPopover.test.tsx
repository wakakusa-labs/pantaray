import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { QuoteCommentPopover } from './QuoteSelectionPopover';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// Lays the popover out 90px tall at the origin, as wide as it was set, as untranslated.
function openAt(top: number, paneRight: number, onAdd = vi.fn()) {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement
  ) {
    const width = parseFloat(this.style.width) || 0;
    return { left: 0, top: 0, right: width, bottom: 90, width, height: 90 } as DOMRect;
  });
  const selection = { text: 'A案は安い。', top, bottom: top + 20, left: 40, endRight: 200 };
  render(
    <UiLanguageProvider initialLanguage="ja">
      <QuoteCommentPopover
        selection={{ ...selection, paneLeft: 0, paneRight }}
        onAdd={onAdd}
        onCancel={vi.fn()}
      />
    </UiLanguageProvider>
  );
  const field = screen.getByRole('textbox', { name: '選んだ部分へのコメント' });
  return { field, popover: field.parentElement! };
}

it('opens below the selection, flips above it without room, and stays inside the pane', () => {
  const below = openAt(100, 1024).popover;
  expect([below.style.width, below.style.transform]).toEqual(['420px', 'translate(40px, 126px)']);
  cleanup();
  // A 300px pane less its 8px margins; 700 - 6 - 90 above the selection.
  const above = openAt(700, 300).popover;
  expect([above.style.width, above.style.transform]).toEqual(['284px', 'translate(8px, 604px)']);
});

it('shows what is quoted, and adds it with the optional comment from 追加', async () => {
  const onAdd = vi.fn();
  const { field } = openAt(100, 1024, onAdd);
  expect(field).toHaveFocus();
  expect(field).toHaveAccessibleDescription('A案は安い。');
  expect(field).toHaveAttribute('placeholder', 'コメント（任意）');
  await userEvent.type(field, '高い');
  await userEvent.click(screen.getByRole('button', { name: '追加' }));
  expect(onAdd).toHaveBeenCalledWith('高い');
});
