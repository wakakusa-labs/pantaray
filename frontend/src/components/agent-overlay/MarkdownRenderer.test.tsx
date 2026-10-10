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

  it('ends a bare file URL where the sentence around it goes on', () => {
    render(
      <MarkdownBlock
        isStreamFinished
        text={
          '（pantaray-file:///Users/name/report.pdf）を開いてください。\n\n' +
          'See "pantaray-file:///Users/name/notes.md",then reply.'
        }
      />
    );

    expect(
      screen.getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')])
    ).toEqual([
      ['report.pdf', 'pantaray-file:///Users/name/report.pdf'],
      ['notes.md', 'pantaray-file:///Users/name/notes.md'],
    ]);
  });

  it('holds back a file URL streamed output may still be cutting', async () => {
    const { container } = render(
      <MarkdownBlock
        isStreamFinished={false}
        text="まず pantaray-file:///Users/name/a.md を見て、次に pantaray-file:///Users/name/repo"
      />
    );

    await waitFor(() => expect(container.textContent).toContain('次に pantaray-file:///'));
    expect(screen.getAllByRole('link').map((link) => link.textContent)).toEqual(['a.md']);
  });

  it('breaks a paragraph and a list item at each single newline, and leaves code as written', () => {
    const { container } = render(
      <MarkdownBlock
        isStreamFinished
        text={
          'よろしくお願いいたします。\n株式会社サンプル  \n山田 太郎\n\n' +
          '- 住所\n  東京都\n\n' +
          '```\nconst a = 1;\nconst b = 2;\n```'
        }
      />
    );

    const [signature] = container.querySelectorAll('p');
    // A hard break (two trailing spaces) stays one break, not two.
    expect(signature.innerHTML).toBe(
      'よろしくお願いいたします。<br>\n株式会社サンプル<br>\n山田 太郎'
    );
    expect(container.querySelector('li')?.innerHTML).toBe('住所<br>\n東京都');
    const code = container.querySelector('pre code');
    expect(code?.textContent).toBe('const a = 1;\nconst b = 2;\n');
    expect(code?.querySelector('br')).toBeNull();
  });
});
