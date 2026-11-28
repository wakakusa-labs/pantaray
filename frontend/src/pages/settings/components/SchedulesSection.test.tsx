import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { hook } from '@/components/schedules/scheduleFixtures';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';

import type { HookEnabledUpdate, HookList } from '../../../../electron/src/settings/hooksFetch';
import { SchedulesSection } from './SchedulesSection';

const ja = (key: MessageKey, vars?: Record<string, string | number>) => translate('ja', key, vars);

function installBridge(lists: HookList[]) {
  let reads = 0;
  const hooks = {
    list: vi.fn(async () => lists[Math.min(reads++, lists.length - 1)]),
    setEnabled: vi.fn(async (_update: HookEnabledUpdate) => undefined),
  };
  window.electron = { hooks } as unknown as Window['electron'];
  return hooks;
}

function renderSection() {
  render(
    <MemoryRouter>
      <UiLanguageProvider initialLanguage="ja">
        <SchedulesSection />
      </UiLanguageProvider>
    </MemoryRouter>
  );
}

describe('SchedulesSection', () => {
  afterEach(() => {
    cleanup();
    delete window.electron;
  });

  // Catches a stopped schedule that hides why it stopped, or whose 「再開する」 switches
  // another schedule or leaves the list showing it stopped.
  it('says why a schedule stopped and runs it again', async () => {
    const stopped = hook({ hook_id: 'task.orders', stopped_reason: 'turn_failed' });
    const bridge = installBridge([
      { hooks: [hook({ hook_id: 'task.weekly' }), stopped], recording_ready: true },
      {
        hooks: [hook({ hook_id: 'task.weekly' }), { ...stopped, stopped_reason: null }],
        recording_ready: true,
      },
    ]);
    renderSection();

    expect(await screen.findByText(ja('schedules.stopped.turnFailed'))).toBeVisible();
    expect(
      screen.getAllByText(ja('schedules.askedIn', { title: '店舗の発注チェック' }))
    ).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: ja('schedules.resume') }));

    await waitFor(() => expect(screen.queryByText(ja('schedules.stopped.turnFailed'))).toBeNull());
    expect(bridge.setEnabled).toHaveBeenCalledWith({ hook_id: 'task.orders', enabled: true });
  });

  // Catches an activity schedule that looks on while recording is off and it sees nothing.
  it('says an activity schedule is not watching while recording is off', async () => {
    installBridge([
      {
        hooks: [
          hook({
            every: null,
            target_action_id: null,
            title: '会議メモ',
            on: 'activity',
            match: [{ field: 'bundle_id', op: 'equals', values: ['us.zoom.xos'] }],
          }),
        ],
        recording_ready: false,
      },
    ]);
    renderSection();

    fireEvent.click(await screen.findByRole('button', { name: /会議メモ/ }));
    expect(screen.getByText(ja('schedules.notWatching'))).toBeVisible();
    expect(screen.getByText(ja('overlay.approvalRequired.hook.newSession'))).toBeVisible();
  });

  // Catches a microphone schedule read as a repeat with no time, or looking on while
  // recording is off and it hears nothing.
  it('words a microphone schedule and says it is not watching while recording is off', async () => {
    installBridge([
      {
        hooks: [
          hook({
            every: null,
            title: '会議の議事録',
            on: 'microphone',
            match: [{ field: 'bundle_id', op: 'equals', values: ['us.zoom.xos'] }],
          }),
        ],
        recording_ready: false,
      },
    ]);
    renderSection();

    fireEvent.click(await screen.findByRole('button', { name: /会議の議事録/ }));
    expect(
      screen.getAllByText('アプリの識別子が『us.zoom.xos』のアプリがマイクを使い始めたとき')
    ).toHaveLength(2);
    expect(screen.getByText(ja('schedules.notWatching'))).toBeVisible();
  });
});
