import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ActionConversationRunItem,
  ActionConversationView as ActionConversationViewModel,
} from '../../../electron/src/actions/actionConversationModel';
import type {
  ActionToolOutputLoader,
  LoadedActionToolOutput,
} from '../../../electron/src/actions/actionToolOutputLoader';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import { ActionConversationView } from './ActionConversationView';
import type { ImageGridCopy } from './AttachedImages';
import { ToolOutputText } from './ToolOutputText';
import { ToolRow } from './ToolRow';
import { resolveToolDisplay, resolveToolLine } from './toolDisplayName';

const OUTPUT_KEY = { actionId: 'action-1', stepId: 'step-tool-1' } as const;
const TOOL_IMAGES_COPY: ImageGridCopy = {
  list: (count) => `${count} screenshots`,
  imageAlt: (position, count) => `Screenshot ${position} of ${count}`,
  open: (position, count) => `Open screenshot ${position} of ${count}`,
  missing: 'Image unavailable',
  lightbox: { dialogLabel: 'Screenshot', close: 'Close', reveal: 'Show in Finder' },
};

/** 描画結果の高さを差し替える。テストの DOM はレイアウトを持たず、素では常に 0 を返す。 */
function measure(
  element: HTMLElement,
  sizes: { scrollHeight: number; clientHeight: number }
): void {
  for (const [name, value] of Object.entries(sizes)) {
    Object.defineProperty(element, name, { configurable: true, value });
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function row(
  loader: ActionToolOutputLoader,
  language: 'en' | 'ja',
  overrides: Partial<ComponentProps<typeof ToolRow>> = {}
) {
  return (
    <UiLanguageProvider initialLanguage={language}>
      <ToolRow
        Icon={resolveToolDisplay('glob', language).Icon}
        line={resolveToolLine('glob', language, {
          subject: '**/*.py',
          running: false,
          outcome: 'completed',
        })}
        preview={null}
        stepNumber={1}
        runLabel={language === 'ja' ? '実行 1: 2026/8/30' : 'Run 1: 8/30/2026'}
        status={null}
        announceStatus={false}
        images={[]}
        imagesCopy={TOOL_IMAGES_COPY}
        outputKey={OUTPUT_KEY}
        loader={loader}
        onFocusedContentUnmount={() => undefined}
        {...overrides}
      />
    </UiLanguageProvider>
  );
}

function renderRow(loader: ActionToolOutputLoader, language: 'en' | 'ja' = 'en') {
  return render(row(loader, language));
}

describe('ToolRow', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('loads the exact Tool identity only after its own row is expanded', async () => {
    const pending = deferred<LoadedActionToolOutput>();
    const load = vi.fn(() => pending.promise);
    const loader: ActionToolOutputLoader = {
      load,
      retry: vi.fn(() => pending.promise),
      clear: vi.fn(),
    };
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:01.000000Z',
      lines: [
        {
          kind: 'tool',
          key: 'run-1:step-tool-1',
          visibility: 'agent_work',
          entry: {
            step_kind: 'tool',
            step_id: 'step-tool-1',
            step_number: 1,
            label: 'read',
            status: 'success',
            outcome: 'completed',
            subject: 'tests/test_retrieval.py',
            output_preview: null,
            output_available: true,
            images: [],
          },
        },
      ],
    };
    const view: ActionConversationViewModel = {
      action: {
        action_id: 'action-1',
        suggestion_id: null,
        status: 'success',
        latest_run_id: 'run-1',
        approved_suggestion: null,
        resumable: false,
      },
      items: [run],
      nextCursor: null,
    };
    render(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView view={view} toolOutputLoader={loader} />
      </UiLanguageProvider>
    );

    expect(load).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 1,/ }));
    expect(load).not.toHaveBeenCalled();
    // 行そのものが開閉ボタン。別に「ツール出力」の行を持たない。
    expect(screen.queryByRole('button', { name: /^Tool output/ })).toBeNull();
    const disclosure = screen.getByRole('button', {
      name: /^Read tests\/test_retrieval.py, Step 1, Run 1:/,
    });

    await userEvent.click(disclosure);

    expect(load).toHaveBeenCalledOnce();
    expect(load).toHaveBeenCalledWith(OUTPUT_KEY);
    expect(disclosure).toHaveFocus();
    expect(screen.getByText('Loading output')).toBeVisible();
    pending.resolve({ kind: 'text', content: 'first\nsecond', truncated: false });
    const outputLabel = /^Tool output, Read tests\/test_retrieval.py, Step 1, Run 1:/;
    await waitFor(() =>
      expect(screen.getByLabelText(outputLabel, { selector: 'pre' })).toHaveTextContent('second')
    );
    expect(screen.getByText('Tool output loaded.')).toHaveAttribute('role', 'status');
    expect(screen.getByLabelText(outputLabel, { selector: 'pre' })).not.toHaveAttribute('role');
    expect(disclosure).toHaveFocus();

    await userEvent.click(disclosure);
    await userEvent.click(disclosure);
    expect(load).toHaveBeenCalledOnce();
  });

  it('keeps failure private and retries only after returning focus to the disclosure', async () => {
    const retryPending = deferred<LoadedActionToolOutput>();
    const load = vi.fn(() => Promise.reject(new Error('private /Users/example/token')));
    const retry = vi.fn(() => retryPending.promise);
    const loader: ActionToolOutputLoader = { load, retry, clear: vi.fn() };
    renderRow(loader, 'ja');
    const disclosure = screen.getByRole('button', {
      name: '**/*.py に合うファイルを探しました, ステップ 1, 実行 1: 2026/8/30',
    });

    await userEvent.click(disclosure);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'ツール出力を読み込めませんでした。'
    );
    expect(screen.queryByText(/private|token|Users\/example/)).toBeNull();
    await userEvent.click(disclosure);
    await userEvent.click(disclosure);
    expect(load).toHaveBeenCalledOnce();
    expect(retry).not.toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole('button', {
        name: /^再試行, \*\*\/\*.py に合うファイルを探しました, ステップ 1, 実行 1:/,
      })
    );
    expect(retry).toHaveBeenCalledOnce();
    expect(retry).toHaveBeenCalledWith(OUTPUT_KEY);
    expect(disclosure).toHaveFocus();
    expect(
      screen.queryByRole('button', {
        name: /^再試行, \*\*\/\*.py に合うファイルを探しました, ステップ 1, 実行 1:/,
      })
    ).toBeNull();
    expect(screen.getByText('ツール出力を読み込み中')).toBeVisible();

    retryPending.resolve({ kind: 'text', content: 'recovered', truncated: false });
    await waitFor(() =>
      expect(
        screen.getByLabelText(
          /^ツール出力, \*\*\/\*.py に合うファイルを探しました, ステップ 1, 実行 1:/,
          {
            selector: 'pre',
          }
        )
      ).toHaveTextContent('recovered')
    );
    expect(screen.getByText('ツール出力を読み込みました。')).toHaveAttribute('role', 'status');
  });

  it.each([
    [{ kind: 'unavailable', reason: 'no_output' }, 'No output is available.'],
    [{ kind: 'unavailable', reason: 'binary' }, 'Binary output cannot be displayed.'],
    [{ kind: 'text', content: 'partial', truncated: true }, 'Output is truncated.'],
  ] as const)('shows the typed output state %#', async (output, expected) => {
    const loader: ActionToolOutputLoader = {
      load: vi.fn(async () => output),
      retry: vi.fn(async () => output),
      clear: vi.fn(),
    };
    renderRow(loader);

    await userEvent.click(
      screen.getByRole('button', {
        name: 'Searched for files matching **/*.py, Step 1, Run 1: 8/30/2026',
      })
    );
    const result = await screen.findByText(expected);
    expect(result).toBeVisible();
    const status = output.kind === 'unavailable' ? result : screen.getByText(/loaded.*truncated/i);
    expect(status).toHaveAttribute('role', 'status');
  });

  it('keeps the live status region in place when a failing step gains its output', () => {
    const loader: ActionToolOutputLoader = {
      load: vi.fn(async () => ({ kind: 'text', content: 'boom', truncated: false }) as const),
      retry: vi.fn(async () => ({ kind: 'text', content: 'boom', truncated: false }) as const),
      clear: vi.fn(),
    };
    // 実行中の行は開けない（processing の出力は必ず unavailable）。
    const { rerender } = render(
      row(loader, 'ja', {
        status: { label: '実行中', failed: false },
        announceStatus: true,
        outputKey: null,
      })
    );
    const announced = screen.getByRole('status');
    expect(announced).toHaveTextContent('実行中');

    // 失敗した行は出力を持つので、同じ更新で開けるようになる。読み上げ領域はそのまま残り、
    // 文字だけが変わる（作り直された領域は、中身を抱えて現れるので読み上げられない）。
    rerender(
      row(loader, 'ja', {
        status: { label: '失敗', failed: true },
        announceStatus: true,
      })
    );
    expect(screen.getByRole('status')).toBe(announced);
    expect(announced).toHaveTextContent('失敗');

    // 焦点を当てただけの利用者にも失敗が届くよう、開閉ボタンの読み上げ名にも状態語を入れる。
    expect(
      screen.getByRole('button', {
        name: '**/*.py に合うファイルを探しました, 失敗, ステップ 1, 実行 1: 2026/8/30',
      })
    ).toBeVisible();
  });

  it('names the result preview a sighted reader can already see on the row', () => {
    const loader: ActionToolOutputLoader = { load: vi.fn(), retry: vi.fn(), clear: vi.fn() };
    render(row(loader, 'en', { preview: 'src/app.py, src/main.py' }));

    expect(
      screen.getByRole('button', {
        name: 'Searched for files matching **/*.py, src/app.py, src/main.py, Step 1, Run 1: 8/30/2026',
      })
    ).toBeVisible();
  });

  it('keeps output in one wrapping box and follows the rendered overflow for focusability', () => {
    let resized: (() => void) | null = null;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          resized = callback;
        }
        observe() {}
        disconnect() {
          resized = null;
        }
      }
    );
    const sixLines = 'one\ntwo\nthree\nfour\nfive\nsix';
    const { rerender } = render(<ToolOutputText content={sixLines} label="Tool output" />);
    const box = screen.getByLabelText('Tool output', { selector: 'pre' });
    expect(box).toHaveTextContent('six');
    // リサイズハンドルを持つ textarea は使わない。オーバーレイの高さが利用者の操作で伸びてしまう。
    expect(document.querySelector('textarea')).toBeNull();

    // 高さ上限に収まっているうちは、行数がいくつでもタブ停止点にしない。
    measure(box, { scrollHeight: 96, clientHeight: 96 });
    act(() => resized?.());
    expect(box).not.toHaveAttribute('tabindex');
    expect(box).not.toHaveAttribute('role');

    // 幅が狭まって折り返しが増え、はみ出した時点で焦点を持たせる。
    measure(box, { scrollHeight: 240, clientHeight: 96 });
    act(() => resized?.());
    expect(screen.getByRole('group', { name: 'Tool output' })).toBe(box);
    expect(box).toHaveAttribute('tabindex', '0');

    // 広がって収まれば、余計なタブ停止点は消す。
    measure(box, { scrollHeight: 96, clientHeight: 96 });
    act(() => resized?.());
    expect(box).not.toHaveAttribute('tabindex');
    expect(screen.queryByRole('group', { name: 'Tool output' })).toBeNull();

    // 大きな出力も間引かない。読めるのはこの箱の中をスクロールした分だけ。
    const largeOutput = Array.from({ length: 10_000 }, (_, index) => `line ${index}`).join('\n');
    rerender(<ToolOutputText content={largeOutput} label="Tool output" />);
    expect(box.textContent).toHaveLength(largeOutput.length);
    expect(box.textContent?.endsWith('line 9999')).toBe(true);
  });
});
