import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { MeetingConsentChip } from './MeetingConsentChip';

const CALL = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y3Z';

function renderChip() {
  const record = vi.fn(async () => 'requested' as const);
  const decline = vi.fn(async () => undefined);
  window.electron = {
    meeting: {
      getCall: vi.fn(async () => ({
        kind: 'awaiting_consent' as const,
        call_id: CALL,
        app: { name: 'Google Chrome', bundle_id: 'com.google.Chrome' },
        site: 'meet.google.com',
        expires_at: '2026-11-28T01:02:00Z',
      })),
      record,
      decline,
    },
    agentOverlay: { resize: vi.fn() },
  } as unknown as Window['electron'];
  render(
    <UiLanguageProvider initialLanguage="ja">
      <MeetingConsentChip />
    </UiLanguageProvider>
  );
  return { record, decline };
}

afterEach(() => {
  cleanup();
  delete window.electron;
});

it('names the call, asks to tell the others, and answers for that call', async () => {
  const { record } = renderChip();
  const panel = await screen.findByRole('region', {
    name: 'meet.google.com の通話を文字起こししますか',
  });
  expect(panel).toHaveAccessibleDescription('相手の同意が必要です');

  fireEvent.click(screen.getByRole('button', { name: '文字起こしする' }));
  expect(record).toHaveBeenCalledWith(CALL);
  // One answer per chip: a second press cannot send another.
  expect(screen.getByRole('button', { name: '今回はしない' })).toBeDisabled();
});

it('has no default button: Enter neither records nor declines', async () => {
  const { record, decline } = renderChip();
  await screen.findByRole('button', { name: '文字起こしする' });
  expect(document.activeElement).toBe(document.body);
  fireEvent.keyDown(document.body, { key: 'Enter' });
  fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
  expect(record).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole('button', { name: '今回はしない' }));
  expect(decline).toHaveBeenCalledWith(CALL);
  expect(record).not.toHaveBeenCalled();
});
