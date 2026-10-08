import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MarkdownBlock } from './MarkdownRenderer';

describe('MarkdownBlock', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    delete window.electron;
  });

  it('reveals pantaray-file links by decoded absolute path without an action', async () => {
    const open = vi.fn(async () => undefined);
    window.electron = { actionFiles: { open } } as unknown as Window['electron'];

    render(
      <MarkdownBlock
        isStreamFinished
        text="[資料フォルダ](pantaray-file:///Users/name/My%20Docs/%E8%B3%87%E6%96%99)"
      />
    );

    fireEvent.click(screen.getByRole('link', { name: '資料フォルダ' }));

    await waitFor(() => {
      expect(open).toHaveBeenCalledWith({ path: '/Users/name/My Docs/資料' });
    });
  });

  it('links a bare file URL in prose by its file name and opens its path', async () => {
    const open = vi.fn(async () => undefined);
    window.electron = { actionFiles: { open } } as unknown as Window['electron'];

    const { container } = render(
      <MarkdownBlock
        isStreamFinished
        text={
          'こちらから開けます。 pantaray-file:///Users/name/sales/顧客リスト_2026-10-09.md\n\n' +
          '控えは（pantaray-file:///Users/name/My%20Docs/%E8%B3%87%E6%96%99.pdf）。見積は' +
          'pantaray-file:///Users/name/見積（改訂）.pdf'
        }
      />
    );

    expect(container.textContent).toBe(
      'こちらから開けます。 顧客リスト_2026-10-09.md\n控えは（資料.pdf）。見積は見積（改訂）.pdf'
    );
    fireEvent.click(screen.getByRole('link', { name: '顧客リスト_2026-10-09.md' }));
    fireEvent.click(screen.getByRole('link', { name: '資料.pdf' }));
    fireEvent.click(screen.getByRole('link', { name: '見積（改訂）.pdf' }));
    await waitFor(() => {
      expect(open.mock.calls).toEqual([
        [{ path: '/Users/name/sales/顧客リスト_2026-10-09.md' }],
        [{ path: '/Users/name/My Docs/資料.pdf' }],
        [{ path: '/Users/name/見積（改訂）.pdf' }],
      ]);
    });
  });

  it('leaves file URLs in code as written and links bare web URLs', () => {
    render(
      <MarkdownBlock
        isStreamFinished
        text={
          '`pantaray-file:///Users/name/a.md` と https://example.com/a\n\n' +
          '```\npantaray-file:///Users/name/b.md\n```'
        }
      />
    );

    expect(screen.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      'https://example.com/a',
    ]);
    expect(screen.getByText('pantaray-file:///Users/name/a.md')).toBeTruthy();
  });
});
