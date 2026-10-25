import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
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
  const askForPermission = vi.fn(async (_permission: string) => undefined);
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
      askForPermission,
    },
  } as unknown as Window['electron'];
  return {
    permissions,
    askForPermission,
    start,
    reportStatus: (enabled: boolean) => statusChanged(enabled),
  };
}

async function renderStep() {
  render(
    <AuthContext.Provider
      value={{ runtimeState: { status: 'ready', owner: OWNER } } as unknown as AuthContextType}
    >
      <LocalOwnerContext.Provider value={OWNER}>
        <UiLanguageProvider initialLanguage="ja">
          <Step />
        </UiLanguageProvider>
      </LocalOwnerContext.Provider>
    </AuthContext.Provider>
  );
  return screen.findByRole('button', { name: '始める' });
}

/** The checklist row with this title. */
const row = (title: string) => within(screen.getByText(title).closest('li')!);
const ACCESSIBILITY = 'アクセシビリティを許可';
const SCREEN_RECORDING = '画面収録を許可';
const START = '記録を始める';

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
  it('asks for each permission from its own row and keeps start disabled until both are granted', async () => {
    const bridge = stubBridge();
    const start = await renderStep();
    expect(start).toBeDisabled();

    const allowAccessibility = row(ACCESSIBILITY).getByRole('button', { name: /^許可する/ });
    fireEvent.click(allowAccessibility);
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('accessibility');
    expect(row(ACCESSIBILITY).getByText('システム設定で Pantaray をオンにします')).toBeVisible();
    // The same button now opens System Settings again, so focus is not lost.
    expect(allowAccessibility).toHaveAccessibleName(`システム設定を開く ${ACCESSIBILITY}`);
    fireEvent.click(allowAccessibility);
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('accessibility');
    expect(row(SCREEN_RECORDING).getByRole('button')).toHaveTextContent('許可する');

    bridge.permissions.accessibility = true;
    await returnFromSystemSettings();
    expect(await row(ACCESSIBILITY).findByText('許可済み')).toBeVisible();
    expect(row(ACCESSIBILITY).queryByRole('button')).not.toBeInTheDocument();
    expect(start).toBeDisabled();

    fireEvent.click(row(SCREEN_RECORDING).getByRole('button', { name: /^許可する/ }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('screen_recording');
    expect(
      row(SCREEN_RECORDING).getByText(
        'システム設定で Pantaray をオンにして、表示に従って開き直します'
      )
    ).toBeVisible();
    expect(start).toBeDisabled();

    bridge.permissions.screenRecording = true;
    await returnFromSystemSettings();
    expect(await row(SCREEN_RECORDING).findByText('許可済み')).toBeVisible();
    expect(start).toBeEnabled();
    expect(row(START).getByText('除外するアプリやサイトは、あとで設定で選べます')).toBeVisible();
  });

  it('shows a start that did not finish on the start row, in place of its note', async () => {
    const start = vi.fn(async () => 'permission_pending');
    const bridge = stubBridge(start);
    Object.assign(bridge.permissions, { accessibility: true, screenRecording: true });
    const button = await renderStep();
    await waitFor(() => expect(button).toBeEnabled());

    fireEvent.click(button);
    expect(await row(START).findByRole('alert')).toHaveTextContent(
      '記録を開始できませんでした。もう一度お試しください。'
    );
    expect(row(START).queryByText('除外するアプリやサイトは、あとで設定で選べます')).toBeNull();
    expect(screen.getByTestId('done')).toHaveTextContent('false');
  });

  it('is not done while a start waits on more permissions, even though the status reads on', async () => {
    const bridge = stubBridge();
    // The recorder is up but not activated: main reports it on and answers the start as pending.
    bridge.start.mockImplementationOnce(async () => {
      bridge.reportStatus(true);
      return 'permission_pending';
    });
    Object.assign(bridge.permissions, { accessibility: true, screenRecording: true });
    const button = await renderStep();
    await waitFor(() => expect(button).toBeEnabled());

    fireEvent.click(button);
    expect(await row(START).findByRole('alert')).toHaveTextContent('記録を開始できませんでした');
    expect(row(START).queryByText('記録中')).not.toBeInTheDocument();
    expect(screen.getByTestId('done')).toHaveTextContent('false');

    // Starting again once the permission is granted finds the recorder activated.
    fireEvent.click(row(START).getByRole('button', { name: '始める' }));
    await waitFor(() => expect(screen.getByTestId('done')).toHaveTextContent('true'));
    expect(row(START).getByText('記録中')).toBeVisible();
    expect(row(START).queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
