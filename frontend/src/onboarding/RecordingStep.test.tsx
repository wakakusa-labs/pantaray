import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { useI18n } from '@/context/useI18n';
import { RecordingStep } from './RecordingStep';
import { useRecordingStep } from './useRecordingStep';

const OWNER = { kind: 'account', id: 'user-1' } as const;

// The parent owns the recording state and reads from it whether the step is done.
function Step() {
  const { t } = useI18n();
  const recording = useRecordingStep();
  return (
    <>
      <RecordingStep t={t} recording={recording} />
      <span data-testid="done">{String(recording.isRecording)}</span>
    </>
  );
}

function stubBridge(start = vi.fn(async () => 'started')) {
  const permissions = { accessibility: false, screenRecording: false };
  const openPermissionSettings = vi.fn(async (_permission: string) => undefined);
  const relaunch = vi.fn(async () => undefined);
  let statusChanged: (enabled: boolean) => void = () => undefined;
  window.electron = {
    screenshot: {
      getStatus: vi.fn(async () => false),
      onStatusChanged: vi.fn((callback: (enabled: boolean) => void) => {
        statusChanged = callback;
        return () => undefined;
      }),
      start,
      stop: vi.fn(async () => true),
      getGateState: vi.fn(async () => ({
        permissions: { ...permissions },
        introRequested: false,
      })),
      openPermissionSettings,
    },
    app: { relaunch },
  } as unknown as Window['electron'];
  return {
    permissions,
    openPermissionSettings,
    relaunch,
    start,
    reportStatus: (enabled: boolean) => statusChanged(enabled),
  };
}

async function renderStep() {
  render(
    <LocalOwnerContext.Provider value={OWNER}>
      <UiLanguageProvider initialLanguage="ja">
        <Step />
      </UiLanguageProvider>
    </LocalOwnerContext.Provider>
  );
  return screen.findByRole('switch', { name: 'コンピューター操作の記録' });
}

// Coming back from System Settings focuses the window, which re-reads the permissions.
async function returnFromSystemSettings() {
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
  });
}

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('RecordingStep', () => {
  it('opens each permission its own pane and keeps the switch off until both are granted', async () => {
    const bridge = stubBridge();
    const toggle = await renderStep();
    expect(toggle).toBeDisabled();
    expect(screen.getByText('2 つの許可が済むとオンにできます')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'アクセシビリティ: 許可する' }));
    expect(bridge.openPermissionSettings).toHaveBeenLastCalledWith('accessibility');
    expect(screen.getByRole('status')).toHaveTextContent(
      'システム設定で Pantaray をオンにしてください'
    );
    fireEvent.click(screen.getByRole('button', { name: 'アクセシビリティ: システム設定を開く' }));
    expect(bridge.openPermissionSettings).toHaveBeenLastCalledWith('accessibility');

    bridge.permissions.accessibility = true;
    await returnFromSystemSettings();
    expect(await screen.findByText('許可済み')).toBeInTheDocument();
    expect(toggle).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '画面収録: 許可する' }));
    expect(bridge.openPermissionSettings).toHaveBeenLastCalledWith('screen_recording');
    expect(toggle).toBeDisabled();

    bridge.permissions.screenRecording = true;
    await returnFromSystemSettings();
    await waitFor(() => expect(screen.getAllByText('許可済み')).toHaveLength(2));
    expect(toggle).toBeEnabled();
    expect(screen.getByText('オンにすると記録を始めます')).toBeInTheDocument();
  });

  it('offers to reopen only after Screen Recording was asked for, and never shows it granted before', async () => {
    const bridge = stubBridge();
    await renderStep();
    const reopen = () => screen.queryByRole('button', { name: 'Pantaray を開き直す' });
    expect(reopen()).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'アクセシビリティ: 許可する' }));
    expect(reopen()).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '画面収録: 許可する' }));
    // The grant made in System Settings reads as missing until the app reopens.
    await returnFromSystemSettings();
    expect(screen.queryByText('許可済み')).not.toBeInTheDocument();

    fireEvent.click(reopen()!);
    expect(bridge.relaunch).toHaveBeenCalledTimes(1);
    // Each relaunch call would start one more instance after this one quits.
    expect(reopen()).toBeDisabled();
  });

  it('starts recording from the switch and says why a start did not finish', async () => {
    const start = vi.fn(async () => 'permission_pending');
    const bridge = stubBridge(start);
    Object.assign(bridge.permissions, { accessibility: true, screenRecording: true });
    const toggle = await renderStep();
    await waitFor(() => expect(toggle).toBeEnabled());

    fireEvent.click(toggle);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'ほかにも許可が必要です。システム設定で許可したら、もう一度スイッチをオンにしてください。'
    );
    expect(start).toHaveBeenCalledTimes(1);

    start.mockResolvedValueOnce('failed');
    fireEvent.click(toggle);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '記録を開始できませんでした。もう一度お試しください。'
    );
  });

  it('is not done while a start waits on more permissions, even though the status reads on', async () => {
    const bridge = stubBridge();
    // The recorder is up but not activated: main reports it on and answers the start as pending.
    bridge.start.mockImplementationOnce(async () => {
      bridge.reportStatus(true);
      return 'permission_pending';
    });
    Object.assign(bridge.permissions, { accessibility: true, screenRecording: true });
    const toggle = await renderStep();
    await waitFor(() => expect(toggle).toBeEnabled());

    fireEvent.click(toggle);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'もう一度スイッチをオンにしてください'
    );
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('done')).toHaveTextContent('false');

    // Turning it on again once the permission is granted finds the recorder activated.
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByTestId('done')).toHaveTextContent('true'));
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
