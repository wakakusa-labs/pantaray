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

type Permission =
  | 'read_accessibility_tree'
  | 'observe_input'
  | 'capture_screen'
  | 'automate_browser'
  | 'automate_safari';

/** `granted` is what main reads for a start; a permission left out is one no start waits on. */
function stubBridge(start = vi.fn(async () => 'started')) {
  const granted: Partial<Record<Permission, boolean>> = {
    read_accessibility_tree: false,
    observe_input: false,
    capture_screen: false,
  };
  const askForPermission = vi.fn(async (_permission: Permission) => undefined);
  const getPermissions = vi.fn(async () =>
    Object.entries(granted).map(([permission, isGranted]) => ({ permission, granted: isGranted }))
  );
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
      getPermissions,
      askForPermission,
    },
  } as unknown as Window['electron'];
  return {
    granted,
    getPermissions,
    askForPermission,
    start,
    grantAll: () =>
      Object.assign(granted, {
        read_accessibility_tree: true,
        observe_input: true,
        capture_screen: true,
      }),
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
const INPUT_MONITORING = '入力監視を許可';
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
  it('asks for each permission from its own row and keeps start disabled until all are granted', async () => {
    const bridge = stubBridge();
    const start = await renderStep();
    await row(SCREEN_RECORDING).findByRole('button');
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      expect.stringContaining(ACCESSIBILITY),
      expect.stringContaining(INPUT_MONITORING),
      expect.stringContaining(SCREEN_RECORDING),
      expect.stringContaining(START),
    ]);
    expect(start).toBeDisabled();

    const allowAccessibility = row(ACCESSIBILITY).getByRole('button', { name: /^許可する/ });
    fireEvent.click(allowAccessibility);
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('read_accessibility_tree');
    expect(row(ACCESSIBILITY).getByText('システム設定で Pantaray をオンにします')).toBeVisible();
    // The same button now opens System Settings, so focus is not lost.
    expect(allowAccessibility).toHaveAccessibleName(`システム設定を開く ${ACCESSIBILITY}`);
    // Accessibility usually covers Input Monitoring, so its row waits for Accessibility.
    expect(row(INPUT_MONITORING).getByRole('button', { name: /^許可する/ })).toBeDisabled();

    fireEvent.click(row(SCREEN_RECORDING).getByRole('button', { name: /^許可する/ }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('capture_screen');
    expect(
      row(SCREEN_RECORDING).getByText(
        'システム設定で Pantaray をオンにして、表示に従って開き直します'
      )
    ).toBeVisible();

    Object.assign(bridge.granted, { read_accessibility_tree: true, capture_screen: true });
    await returnFromSystemSettings();
    expect(await row(ACCESSIBILITY).findByText('許可済み')).toBeVisible();
    expect(row(SCREEN_RECORDING).getByText('許可済み')).toBeVisible();
    expect(row(ACCESSIBILITY).queryByRole('button')).not.toBeInTheDocument();
    expect(start).toBeDisabled();

    fireEvent.click(row(INPUT_MONITORING).getByRole('button', { name: /^許可する/ }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('observe_input');

    bridge.granted.observe_input = true;
    await returnFromSystemSettings();
    expect(await row(INPUT_MONITORING).findByText('許可済み')).toBeVisible();
    expect(start).toBeEnabled();
    expect(row(START).getByText('除外するアプリやサイトは、あとで設定で選べます')).toBeVisible();
  });

  it('lists only the permissions main says a start waits on', async () => {
    const bridge = stubBridge();
    delete bridge.granted.observe_input;
    // Main lists Chrome only once it was denied, until it is turned on again.
    Object.assign(bridge.granted, {
      read_accessibility_tree: true,
      capture_screen: true,
      automate_browser: false,
    });
    const start = await renderStep();
    fireEvent.click(await screen.findByRole('button', { name: /^許可する Chrome の操作を許可/ }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('automate_browser');
    expect(screen.queryByText(INPUT_MONITORING)).not.toBeInTheDocument();
    expect(start).toBeDisabled();

    delete bridge.granted.automate_browser;
    await returnFromSystemSettings();
    await waitFor(() => expect(start).toBeEnabled());
    expect(screen.queryByText('Chrome の操作を許可')).not.toBeInTheDocument();
  });

  it('says recording is unavailable when the permissions cannot be read', async () => {
    const bridge = stubBridge();
    bridge.getPermissions.mockRejectedValue(new Error('doctor failed'));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const start = await renderStep();
    expect(await row(START).findByRole('alert')).toHaveTextContent(
      '現在はコンピューター操作の記録を有効にできません。'
    );
    expect(start).toBeDisabled();
  });

  it('shows a start that did not finish on the start row, in place of its note', async () => {
    const start = vi.fn(async () => 'failed');
    const bridge = stubBridge(start);
    bridge.grantAll();
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
    bridge.grantAll();
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
