import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';

import type { HookEnabledUpdate, HookList } from '../../../electron/src/settings/hooksFetch';
import { ConversationSchedules } from './ConversationSchedules';
import { hook } from './scheduleFixtures';
import { SCHEDULES_POLL_MS } from './useSchedules';

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

function renderStrip(actionId: string) {
  render(
    <UiLanguageProvider initialLanguage="ja">
      <ConversationSchedules actionId={actionId} />
    </UiLanguageProvider>
  );
}

const meeting = hook({
  hook_id: 'task.meeting',
  every: null,
  target_action_id: null,
  title: '会議メモ',
  message: '会議のメモを取ってください。',
  on: 'activity',
  match: [{ field: 'bundle_id', op: 'equals', values: ['us.zoom.xos'] }],
});

describe('ConversationSchedules', () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    delete window.electron;
  });

  // Catches a strip that shows another conversation's schedules, or drops one asked for here
  // that starts a new conversation each time and so has no other conversation to show in.
  it('shows the schedules that continue this conversation and those asked for here', async () => {
    installBridge([
      {
        hooks: [
          hook({ hook_id: 'task.daily', offset: '450m' }),
          meeting,
          hook({
            hook_id: 'task.elsewhere',
            created_from_action_id: 'action-2',
            target_action_id: 'action-2',
            message: '別の会話の予定です。',
          }),
        ],
        recording_ready: false,
      },
    ]);
    renderStrip('action-1');

    const strip = await screen.findByRole('list', { name: ja('schedules.strip.label') });
    const lines = within(strip).getAllByRole('listitem');
    expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveTextContent('毎日 7:30、この会話を続けます。');
    expect(lines[1]).toHaveTextContent(
      'アプリの識別子が『us.zoom.xos』のウィンドウになったとき、毎回新しい会話を始めます。'
    );
    // Recording is off, so the window schedule says it sees nothing for now.
    expect(lines[1]).toHaveTextContent(ja('schedules.notWatching'));
    expect(lines[0]).not.toHaveTextContent(ja('schedules.notWatching'));
  });

  // Catches a microphone schedule with no condition read as a repeat with no time, or
  // looking on while recording is off and it hears nothing.
  it('words a microphone schedule with no condition, and says it is not watching', async () => {
    installBridge([
      {
        hooks: [
          hook({ hook_id: 'task.daily', offset: '450m' }),
          hook({ hook_id: 'task.mic', every: null, on: 'microphone', match: [] }),
        ],
        recording_ready: false,
      },
    ]);
    renderStrip('action-1');

    const strip = await screen.findByRole('list', { name: ja('schedules.strip.label') });
    const lines = within(strip).getAllByRole('listitem');
    expect(lines[1]).toHaveTextContent(
      'どれかのアプリがマイクを使い始めたとき、この会話を続けます。'
    );
    expect(lines[1]).toHaveTextContent(ja('schedules.notWatching'));
    // A repeat still reads as one and needs no recording.
    expect(lines[0]).toHaveTextContent('毎日 7:30、この会話を続けます。');
    expect(lines[0]).not.toHaveTextContent(ja('schedules.notWatching'));
  });

  // Catches 「再開する」 that switches another schedule, or a strip still showing it stopped.
  it('runs a stopped schedule again', async () => {
    const stopped = hook({ stopped_reason: 'turn_failed', offset: '450m' });
    const bridge = installBridge([
      { hooks: [stopped], recording_ready: true },
      { hooks: [{ ...stopped, stopped_reason: null }], recording_ready: true },
    ]);
    renderStrip('action-1');

    expect(
      await screen.findByText(ja('schedules.strip.stoppedTurn', { when: '毎日 7:30' }))
    ).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: ja('schedules.resume') }));

    expect(await screen.findByRole('switch', { name: ja('schedules.switch') })).toBeChecked();
    expect(bridge.setEnabled).toHaveBeenCalledWith({ hook_id: 'task.one', enabled: true });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: ja('schedules.resume') })).toBeNull()
    );
  });

  // Catches a strip that shows a schedule on after it stopped: the stop lands a few seconds
  // after its turn ends, with nothing else for the window to hear.
  it('shows a schedule that stopped after its turn without being told', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const daily = hook({ offset: '450m' });
    installBridge([
      { hooks: [daily], recording_ready: true },
      { hooks: [{ ...daily, stopped_reason: 'turn_failed' }], recording_ready: true },
    ]);
    renderStrip('action-1');
    expect(await screen.findByRole('switch', { name: ja('schedules.switch') })).toBeChecked();

    act(() => vi.advanceTimersByTime(SCHEDULES_POLL_MS));

    expect(await screen.findByRole('button', { name: ja('schedules.resume') })).toBeVisible();
    expect(screen.queryByRole('switch')).toBeNull();
  });
});
