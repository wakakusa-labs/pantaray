import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';
import { RecordingControls } from './RecordingControls';

function Controls() {
  return <RecordingControls capture={useScreenshotCaptureStatus()} onOpenSettings={() => {}} />;
}

function renderControl(language: 'ja' | 'en') {
  return render(
    <UiLanguageProvider initialLanguage={language}>
      <MemoryRouter>
        <Controls />
      </MemoryRouter>
    </UiLanguageProvider>
  );
}

describe('RecordingControls', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    localStorage.clear();
    delete window.electron;
  });

  it('turns recording on without any filter prerequisite', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    const start = vi.fn(async () => 'started');
    window.electron = {
      screenshot: {
        start,
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => false),
        onStatusChanged: vi.fn(() => () => undefined),
      },
    } as unknown as Window['electron'];

    renderControl('ja');

    const toggle = await screen.findByRole('switch', { name: 'コンピューター操作の記録' });
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(start).toHaveBeenCalledTimes(1);
    });
  });

  it('says suggestions are paused too while recording is paused', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => false),
        onStatusChanged: vi.fn(() => () => undefined),
      },
    } as unknown as Window['electron'];

    renderControl('ja');

    expect(await screen.findByText('一時停止・提案も止まっています')).toBeInTheDocument();
  });

  it('shows no switch until the capture status arrives, then renders it on', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    let resolveStatus: (isCapturing: boolean) => void = () => undefined;
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(
          () =>
            new Promise<boolean>((resolve) => {
              resolveStatus = resolve;
            })
        ),
        onStatusChanged: vi.fn(() => () => undefined),
      },
    } as unknown as Window['electron'];

    renderControl('ja');

    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(screen.getByText('状態を取得中…')).toBeInTheDocument();

    resolveStatus(true);

    const toggle = await screen.findByRole('switch', { name: 'コンピューター操作の記録' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(toggle.className).toContain('settings-toggle--on');
  });
  it('shows status failure without claiming recording is paused', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => {
          throw new Error('unavailable');
        }),
        onStatusChanged: vi.fn(() => () => undefined),
      },
    } as unknown as Window['electron'];
    renderControl('ja');
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '現在はコンピューター操作の記録を有効にできません。'
    );
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(screen.queryByText('一時停止・提案も止まっています')).not.toBeInTheDocument();
  });
});
