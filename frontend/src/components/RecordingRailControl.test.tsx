import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { RecordingRailControl } from './RecordingRailControl';

function CurrentLocation() {
  const location = useLocation();
  return <output aria-label="location">{`${location.pathname}${location.search}`}</output>;
}

function renderControl(language: 'ja' | 'en') {
  return render(
    <UiLanguageProvider initialLanguage={language}>
      <MemoryRouter initialEntries={['/history']}>
        <RecordingRailControl />
        <Routes>
          <Route path="*" element={<CurrentLocation />} />
        </Routes>
      </MemoryRouter>
    </UiLanguageProvider>
  );
}

function railButton() {
  return screen.getByRole('button', { name: /コンピューター操作の記録/ });
}

function openPopover() {
  fireEvent.click(railButton());
  return screen.getByRole('dialog', { name: 'コンピューター操作の記録' });
}

describe('RecordingRailControl', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    localStorage.clear();
    delete window.electron;
  });

  it('lights the rail only while recording is on, following status changes', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    let notify: (isEnabled: boolean) => void = () => undefined;
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => false),
        onStatusChanged: vi.fn((callback: (isEnabled: boolean) => void) => {
          notify = callback;
          return () => undefined;
        }),
      },
    } as unknown as Window['electron'];

    renderControl('ja');

    const button = await screen.findByRole('button', { name: 'コンピューター操作の記録：オフ' });
    const light = button.querySelector('.app-recording-light');
    expect(light).not.toHaveClass('app-recording-light--on');

    act(() => notify(true));

    expect(button).toHaveAccessibleName('コンピューター操作の記録：オン');
    expect(light).toHaveClass('app-recording-light--on');
  });

  it('closes on Escape back to the rail, and the filter link opens its settings', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => true),
        onStatusChanged: vi.fn(() => () => undefined),
      },
    } as unknown as Window['electron'];

    renderControl('ja');
    await screen.findByRole('button', { name: 'コンピューター操作の記録：オン' });

    openPopover();
    expect(railButton()).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(railButton()).toHaveFocus();

    openPopover();
    fireEvent.click(screen.getByRole('button', { name: '記録するアプリとウェブサイト' }));
    expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent(
      '/settings?section=screenshots'
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('leaves focus where the user put it while a toggle is answered', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    let notify: (isEnabled: boolean) => void = () => undefined;
    let finishStart: (result: string) => void = () => undefined;
    window.electron = {
      screenshot: {
        start: vi.fn(
          () =>
            new Promise<string>((resolve) => {
              finishStart = resolve;
            })
        ),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => false),
        onStatusChanged: vi.fn((callback: (isEnabled: boolean) => void) => {
          notify = callback;
          return () => undefined;
        }),
      },
    } as unknown as Window['electron'];

    renderControl('ja');
    await screen.findByRole('button', { name: 'コンピューター操作の記録：オフ' });
    openPopover();
    const toggle = await screen.findByRole('switch', { name: 'コンピューター操作の記録' });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toBeDisabled());

    const link = screen.getByRole('button', { name: '記録するアプリとウェブサイト' });
    link.focus();
    await act(async () => {
      finishStart('started');
    });
    act(() => notify(true));

    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(link).toHaveFocus();
  });

  it('marks the light and opens the recording settings only while recording waits on a permission', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    let reportPermissionRequired: (isRequired: boolean) => void = () => undefined;
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => true),
        onStatusChanged: vi.fn(() => () => undefined),
        getPermissionRequired: vi.fn(async () => false),
        onPermissionRequiredChanged: vi.fn((callback: (isRequired: boolean) => void) => {
          reportPermissionRequired = callback;
          return () => undefined;
        }),
      },
    } as unknown as Window['electron'];

    renderControl('ja');
    const button = await screen.findByRole('button', { name: 'コンピューター操作の記録：オン' });
    expect(button.querySelector('.app-recording-alert')).toBeNull();

    const light = button.querySelector('.app-recording-light');
    expect(light).toHaveClass('app-recording-light--on');

    act(() => reportPermissionRequired(true));
    expect(button).toHaveAccessibleName('コンピューター操作の記録：許可が必要です');
    expect(button.querySelector('.app-recording-alert')).not.toBeNull();
    // Recording is on, but it does not run: only the warning shows.
    expect(light).not.toHaveClass('app-recording-light--on');
    expect(button).not.toHaveAttribute('aria-haspopup');

    fireEvent.click(button);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent(
      '/settings?section=screenshots'
    );

    act(() => reportPermissionRequired(false));
    expect(button.querySelector('.app-recording-alert')).toBeNull();
    expect(light).toHaveClass('app-recording-light--on');
    fireEvent.click(button);
    expect(screen.getByRole('dialog', { name: 'コンピューター操作の記録' })).toBeVisible();
  });

  it('shows a transcribed call with Stop, and offers a passed-up call while it lasts', async () => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    const CALL = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y3Z';
    const offered = {
      call_id: CALL,
      app: { name: 'zoom.us', bundle_id: 'us.zoom.xos' },
      site: null,
    };
    let changeCall: (call: unknown) => void = () => undefined;
    const meeting = {
      getCall: vi.fn(async () => ({ kind: 'recording', started_at: 'now', ...offered })),
      onCallChanged: vi.fn((callback: (call: unknown) => void) => {
        changeCall = callback;
        return () => undefined;
      }),
      record: vi.fn(async () => 'requested'),
      stop: vi.fn(async () => 'requested'),
    };
    window.electron = {
      screenshot: {
        start: vi.fn(async () => 'started'),
        stop: vi.fn(async () => true),
        getStatus: vi.fn(async () => true),
        onStatusChanged: vi.fn(() => () => undefined),
      },
      meeting,
    } as unknown as Window['electron'];

    renderControl('ja');
    const button = await screen.findByRole('button', { name: 'zoom.us の通話を文字起こし中' });
    expect(button.querySelector('.app-recording-light')).toHaveClass(
      'app-recording-light--transcribing'
    );
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: '止める' }));
    expect(meeting.stop).toHaveBeenCalledWith(CALL);

    act(() => changeCall({ kind: 'not_recording', refused: null, ...offered }));
    expect(button).toHaveAccessibleName('コンピューター操作の記録：オン');
    expect(button.querySelector('.app-recording-light')).not.toHaveClass(
      'app-recording-light--transcribing'
    );
    fireEvent.click(screen.getByRole('button', { name: 'この通話を文字起こしする' }));
    expect(meeting.record).toHaveBeenCalledWith(CALL);

    // The call ended, or still waits on the chip: nothing to offer here.
    for (const call of [
      { kind: 'idle' },
      { kind: 'awaiting_consent', expires_at: 'later', ...offered },
    ]) {
      act(() => changeCall(call));
      expect(screen.queryByRole('button', { name: 'この通話を文字起こしする' })).toBeNull();
      expect(screen.queryByRole('button', { name: '止める' })).toBeNull();
    }
  });
});
