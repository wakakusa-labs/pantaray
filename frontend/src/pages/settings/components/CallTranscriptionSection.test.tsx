import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { t } from '@/i18n/translate';
import type { MeetingSettings } from '../../../../electron/src/context/meetingTranscript';
import { CallTranscriptionSection } from './CallTranscriptionSection';

const SETTINGS: MeetingSettings = { locale: null };

function renderSection({
  supported = true,
  updateSettings = vi.fn(
    async (_settings: MeetingSettings): Promise<'saved' | 'restart_failed'> => 'saved'
  ),
} = {}) {
  const meeting = {
    getSettings: vi.fn(async () => ({ settings: SETTINGS, supported })),
    getAvailability: vi.fn(async () => ({ action_required: ['audio_capture'] })),
    updateSettings,
    prepare: vi.fn(async () => 'requested'),
  };
  window.electron = { meeting } as unknown as Window['electron'];
  render(
    <UiLanguageProvider initialLanguage="ja">
      <LocalOwnerContext.Provider value={{ kind: 'guest', id: 'owner-a' }}>
        <CallTranscriptionSection t={(key, vars) => t('ja', key, vars)} />
      </LocalOwnerContext.Provider>
    </UiLanguageProvider>
  );
  return meeting;
}

const chooseLanguage = (value: string) =>
  fireEvent.change(screen.getByRole('combobox', { name: '言語' }), { target: { value } });

afterEach(() => {
  cleanup();
  delete window.electron;
});

it('on a Mac that cannot transcribe, only says where it works', async () => {
  renderSection({ supported: false });
  expect(
    await screen.findByText('macOS 26 以降の Apple シリコンの Mac で使えます。')
  ).toBeInTheDocument();
  expect(screen.queryByText(/文字起こしするか確認します/)).toBeNull();
  expect(screen.queryByRole('combobox')).toBeNull();
});

it('shows each permission, with System Settings for the one still missing', async () => {
  renderSection();
  expect(await screen.findByText('システムオーディオ録音')).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: /システム設定を開く システムオーディオ録音/ })
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: /システム設定を開く マイク/ })).toBeNull();
});

it('an edit made while a slow save runs cannot undo that save', async () => {
  let finishSave: () => void = () => undefined;
  const updateSettings = vi.fn(
    (_settings: MeetingSettings) =>
      new Promise<'saved' | 'restart_failed'>((resolve) => {
        finishSave = () => resolve('saved');
      })
  );
  renderSection({ updateSettings });
  await screen.findByRole('combobox', { name: '言語' });

  chooseLanguage('ja-JP');
  // The language waits for that save to finish.
  expect(screen.getByRole('combobox', { name: '言語' })).toBeDisabled();
  chooseLanguage('en-US');
  expect(updateSettings).toHaveBeenCalledTimes(1);

  await act(async () => finishSave());
  expect(screen.getByRole('combobox', { name: '言語' })).toHaveValue('ja-JP');
  chooseLanguage('en-US');
  expect(updateSettings).toHaveBeenLastCalledWith({ ...SETTINGS, locale: 'en-US' });
});

it('keeps a change saved before recording failed to come back, and retries with it', async () => {
  const updateSettings = vi
    .fn<(settings: MeetingSettings) => Promise<'saved' | 'restart_failed'>>()
    .mockResolvedValueOnce('restart_failed')
    .mockResolvedValue('saved');
  renderSection({ updateSettings });
  await screen.findByRole('combobox', { name: '言語' });

  chooseLanguage('ja-JP');
  fireEvent.click(await screen.findByRole('button', { name: '記録を再開' }));
  expect(screen.getByRole('combobox', { name: '言語' })).toHaveValue('ja-JP');
  await waitFor(() => expect(screen.queryByRole('button', { name: '記録を再開' })).toBeNull());
  expect(updateSettings).toHaveBeenLastCalledWith({ ...SETTINGS, locale: 'ja-JP' });
});
