import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SETTINGS_MESSAGES } from '@/i18n/messageCatalog/settings';
import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';
import { OverlayPositionSection } from './OverlayPositionSection';

type Placements = Awaited<
  ReturnType<NonNullable<NonNullable<Window['electron']>['overlayPlacement']>['get']>
>;

const ja = (key: MessageKey, vars?: Record<string, string | number>) => translate('ja', key, vars);

const DEFAULTS: Placements = {
  suggestion: { row: 0, column: 4 },
  started: { row: 1, column: 2 },
  history: { row: 1, column: 2 },
};

function installBridge(
  set = vi.fn(async (update: { kind: string; cell: object }) => ({
    ...DEFAULTS,
    [update.kind]: update.cell,
  }))
) {
  const overlayPlacement = { get: vi.fn(async () => DEFAULTS), set };
  window.electron = { overlayPlacement } as unknown as Window['electron'];
  return overlayPlacement;
}

// happy-dom lays nothing out, so the miniature screen is given a 160 x 100 box at (0, 0).
function layOutScreen(slider: HTMLElement) {
  const screenElement = slider.parentElement!;
  screenElement.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 160, height: 100, right: 160, bottom: 100 }) as DOMRect;
  return screenElement;
}

describe('OverlayPositionSection', () => {
  afterEach(() => {
    cleanup();
    delete window.electron;
  });

  it("names the section and each kind by what appears, without the window's internal name", async () => {
    installBridge();
    render(<OverlayPositionSection t={ja} />);

    expect(screen.getByRole('heading', { name: '提案や作業を出す位置' })).toBeInTheDocument();
    expect(translate('en', 'settings.overlayPosition.title')).toBe(
      'Where suggestions and tasks appear'
    );
    for (const language of ['ja', 'en'] as const)
      for (const key of Object.keys(SETTINGS_MESSAGES[language]).filter((name) =>
        name.startsWith('settings.overlayPosition.')
      ))
        expect(translate(language, key as MessageKey)).not.toMatch(
          /オーバーレイ|パネル|overlay|panel/iu
        );

    const suggestion = await screen.findByRole('slider', { name: '届いた提案の位置' });
    expect(suggestion).toHaveAttribute('aria-valuetext', '上段・右端');
    expect(screen.getByRole('slider', { name: '始めた作業の位置' })).toHaveAttribute(
      'aria-valuetext',
      '中段・中央'
    );
    expect(screen.getByRole('slider', { name: '一覧から開いた作業の位置' })).toHaveAttribute(
      'aria-valuetext',
      '中段・中央'
    );
  });

  it('moves one cell per arrow key, stops at the edges, and saves only that kind', async () => {
    const bridge = installBridge();
    render(<OverlayPositionSection t={ja} />);
    const suggestion = await screen.findByRole('slider', { name: '届いた提案の位置' });
    suggestion.focus();

    fireEvent.keyDown(suggestion, { key: 'ArrowRight' });
    fireEvent.keyDown(suggestion, { key: 'ArrowUp' });
    expect(bridge.set).not.toHaveBeenCalled();

    fireEvent.keyDown(suggestion, { key: 'ArrowDown' });
    expect(suggestion).toHaveAttribute('aria-valuetext', '中段・右端');
    fireEvent.keyDown(suggestion, { key: 'ArrowLeft' });
    expect(suggestion).toHaveAttribute('aria-valuetext', '中段・右寄り');
    expect(bridge.set.mock.calls).toEqual([
      [{ kind: 'suggestion', cell: { row: 1, column: 4 } }],
      [{ kind: 'suggestion', cell: { row: 1, column: 3 } }],
    ]);
    expect(screen.getByRole('slider', { name: '始めた作業の位置' })).toHaveAttribute(
      'aria-valuetext',
      '中段・中央'
    );
  });

  it('snaps the dragged block to the cell under the pointer and saves it on drop', async () => {
    const bridge = installBridge();
    render(<OverlayPositionSection t={ja} />);
    const started = await screen.findByRole('slider', { name: '始めた作業の位置' });
    const screenElement = layOutScreen(started);

    fireEvent.pointerDown(screenElement, { button: 0, pointerId: 1, clientX: 80, clientY: 50 });
    expect(started).toHaveFocus();
    fireEvent.pointerMove(screenElement, { pointerId: 1, clientX: 5, clientY: 95 });
    expect(started).toHaveAttribute('aria-valuetext', '下段・左端');
    expect(bridge.set).not.toHaveBeenCalled();
    // Past the edge of the screen it stays in the last cell.
    fireEvent.pointerMove(screenElement, { pointerId: 1, clientX: 400, clientY: -30 });
    expect(started).toHaveAttribute('aria-valuetext', '上段・右端');
    fireEvent.pointerUp(screenElement, { pointerId: 1, clientX: 110, clientY: 99 });

    expect(started).toHaveAttribute('aria-valuetext', '下段・右寄り');
    expect(bridge.set.mock.calls).toEqual([[{ kind: 'started', cell: { row: 2, column: 3 } }]]);

    // A drop back on the same cell saves nothing.
    fireEvent.pointerDown(screenElement, { button: 0, pointerId: 2, clientX: 110, clientY: 99 });
    fireEvent.pointerUp(screenElement, { pointerId: 2, clientX: 110, clientY: 99 });
    expect(bridge.set).toHaveBeenCalledTimes(1);
  });

  it('returns to the saved position and says so when saving fails', async () => {
    let rejectSave: (error: Error) => void = () => {};
    installBridge(
      vi.fn(
        () =>
          new Promise<Placements>((_resolve, reject) => {
            rejectSave = reject;
          })
      )
    );
    render(<OverlayPositionSection t={ja} />);
    const history = await screen.findByRole('slider', { name: '一覧から開いた作業の位置' });

    fireEvent.keyDown(history, { key: 'ArrowDown' });
    expect(history).toHaveAttribute('aria-valuetext', '下段・中央');
    await act(async () => rejectSave(new Error('disk full')));

    await waitFor(() => expect(history).toHaveAttribute('aria-valuetext', '中段・中央'));
    expect(screen.getByRole('alert')).toHaveTextContent(
      '保存できませんでした。以前の位置のままです。'
    );
  });
});
