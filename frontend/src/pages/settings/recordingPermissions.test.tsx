import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { CapturePermissionCheck } from '../../../electron/src/context/zaneiProcess';
import SettingsPage from '../SettingsPage';

const OWNER = { kind: 'account', id: 'user-1' } as const;

function stubBridge(checks: CapturePermissionCheck[], permissionRequired = false) {
  let reportPermissionRequired: (isRequired: boolean) => void = () => undefined;
  const bridge = {
    getPermissions: vi.fn(async () => checks),
    askForPermission: vi.fn(async () => undefined),
    getPermissionRequired: vi.fn(async () => permissionRequired),
  };
  window.electron = {
    privacy: {
      getCaptureSettings: vi.fn(async () => ({
        version: 2,
        apps: { mode: 'exclude', entries: [] },
        websites: { mode: 'exclude', hosts: [] },
        ideFileRules: {
          mode: 'on',
          onFileNameUnavailable: 'allow',
          sensitivePresets: { blockEnvFiles: true },
        },
      })),
      onCaptureSettingsUpdated: vi.fn(() => () => undefined),
      listInstalledApps: vi.fn(async () => []),
    },
    screenshot: {
      getStatus: vi.fn(async () => true),
      onStatusChanged: vi.fn(() => () => undefined),
      start: vi.fn(async () => 'started'),
      stop: vi.fn(async () => true),
      onPermissionRequiredChanged: vi.fn((callback: (isRequired: boolean) => void) => {
        reportPermissionRequired = callback;
        return () => undefined;
      }),
      ...bridge,
    },
  } as unknown as Window['electron'];
  return { ...bridge, reportPermissionRequired: (next: boolean) => reportPermissionRequired(next) };
}

function renderRecordingSection() {
  render(
    <AuthContext.Provider
      value={{ runtimeState: { status: 'ready', message: null, owner: OWNER } } as AuthContextType}
    >
      <LocalOwnerContext.Provider value={OWNER}>
        <UiLanguageProvider initialLanguage="ja">
          <MemoryRouter initialEntries={['/settings?section=screenshots']}>
            <SettingsPage />
          </MemoryRouter>
        </UiLanguageProvider>
      </LocalOwnerContext.Provider>
    </AuthContext.Provider>
  );
}

const row = (title: string) => within(screen.getByText(title).closest('li')!);

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('Settings recording permissions', () => {
  it('lists every permission main reports under the switch, even when all are granted', async () => {
    stubBridge([
      { permission: 'read_accessibility_tree', granted: true },
      { permission: 'observe_input', granted: true },
      { permission: 'capture_screen', granted: true },
    ]);
    renderRecordingSection();

    expect(await screen.findByText('アクセシビリティ')).toBeVisible();
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'アクセシビリティ許可済み',
      '入力監視許可済み',
      '画面収録許可済み',
    ]);
    // The switch stays the control: Settings has no start row.
    expect(screen.queryByRole('button', { name: '始める' })).not.toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'コンピューター操作の記録' })).toBeChecked();
    expect(screen.getByText('有効')).toBeVisible();
  });

  it('asks for a missing permission from its row, then waits on System Settings', async () => {
    const bridge = stubBridge([
      { permission: 'read_accessibility_tree', granted: true },
      { permission: 'observe_input', granted: false },
      { permission: 'capture_screen', granted: true },
      { permission: 'automate_browser', granted: false },
    ]);
    renderRecordingSection();

    fireEvent.click(await screen.findByRole('button', { name: '許可する 入力監視' }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('observe_input');
    expect(row('入力監視').getByText(/^システム設定で Pantaray をオンにします/)).toBeVisible();
    expect(row('入力監視').getByRole('button')).toHaveAccessibleName('システム設定を開く 入力監視');

    fireEvent.click(screen.getByRole('button', { name: '許可する Chrome の操作' }));
    expect(bridge.askForPermission).toHaveBeenLastCalledWith('automate_browser');
  });

  it('says recording needs a permission while the recorder reports it', async () => {
    const bridge = stubBridge([{ permission: 'capture_screen', granted: false }], true);
    renderRecordingSection();

    expect(await screen.findByText('許可が必要です')).toBeVisible();
    expect(screen.queryByText('有効')).not.toBeInTheDocument();

    act(() => bridge.reportPermissionRequired(false));
    expect(screen.getByText('有効')).toBeVisible();
  });
});
