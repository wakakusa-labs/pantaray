import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { FilePreviewPane } from './FilePreviewPane';
import { taskFileAt } from './taskFiles';

type ActionFiles = NonNullable<NonNullable<Window['electron']>['actionFiles']>;

const read = vi.fn<ActionFiles['read']>();

function renderPreview(path: string, text: string) {
  read.mockResolvedValue({ kind: 'text', text, truncated: false });
  render(
    <UiLanguageProvider initialLanguage="en">
      <FilePreviewPane
        target={{ kind: 'action', id: 'act-1' }}
        file={taskFileAt(path)}
        revision={null}
        onClose={() => {}}
      />
    </UiLanguageProvider>
  );
}

beforeEach(() => {
  read.mockReset();
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: { actionFiles: { read } },
  });
});

afterEach(cleanup);

describe('FilePreviewPane', () => {
  it('shows each file of a patch as a diff, as the steps show an edit', async () => {
    renderPreview(
      '/work/fix.patch',
      [
        'diff --git a/src/app.ts b/src/app.ts',
        'index 1111111..2222222 100644',
        '--- a/src/app.ts',
        '+++ b/src/app.ts',
        '@@ -1,2 +1,2 @@',
        ' keep',
        '-old',
        '+new',
        'diff --git a/README.md b/README.md',
        '--- a/README.md',
        '+++ b/README.md',
        '@@ -3 +3,2 @@',
        ' intro',
        '+more',
        '',
      ].join('\n')
    );

    const tables = await screen.findAllByRole('table');
    expect(
      [...document.querySelectorAll('.action-conversation__diff-path')].map(
        (path) => path.textContent
      )
    ).toEqual(['src/app.ts', 'README.md']);
    const rows = (table: HTMLElement) =>
      within(table)
        .getAllByRole('row')
        .slice(1)
        .map((row) => row.textContent);
    expect(tables.map(rows)).toEqual([
      ['11 keep', '2−Removed: old', '2+Added: new'],
      ['33 intro', '4+Added: more'],
    ]);
    expect(document.querySelector('pre')).toBeNull();
  });

  it('keeps a .diff that is not a diff as plain text', async () => {
    const text = 'Notes on the change\n--- see below\nnothing more';
    renderPreview('/work/notes.diff', text);

    expect((await screen.findByText(/Notes on the change/)).tagName).toBe('PRE');
    expect(screen.getByText(/Notes on the change/).textContent).toBe(text);
    expect(screen.queryByRole('table')).toBeNull();
  });
});
