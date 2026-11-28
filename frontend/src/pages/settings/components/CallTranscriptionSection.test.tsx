import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { t } from '@/i18n/translate';
import type {
  MeetingCallLists,
  MeetingSettings,
} from '../../../../electron/src/context/meetingTranscript';
import { CallTranscriptionSection } from './CallTranscriptionSection';

const SETTINGS: MeetingSettings = { callApps: null, callSites: null, locale: null };
const DEFAULTS = { callApps: ['us.zoom.xos'], callSites: ['meet.google.com', 'zoom.us'] };

function renderSection({
  supported = true,
  getDefaultCallLists = vi.fn(async (): Promise<MeetingCallLists> => DEFAULTS),
  updateSettings = vi.fn(async (_settings: MeetingSettings) => undefined),
  setCaptureEditing = vi.fn(async (_request: { kind: string }) => true),
} = {}) {
  const meeting = {
    getSettings: vi.fn(async () => ({ settings: SETTINGS, supported })),
    getDefaultCallLists,
    getAvailability: vi.fn(async () => ({ action_required: ['audio_capture'] })),
    updateSettings,
    prepare: vi.fn(async () => 'requested'),
  };
  window.electron = {
    meeting,
    privacy: {
      listInstalledApps: vi.fn(async () => [
        { name: 'Zoom', bundleId: 'us.zoom.xos', iconDataUrl: null },
      ]),
      setCaptureEditing,
    },
  } as unknown as Window['electron'];
  render(
    <UiLanguageProvider initialLanguage="ja">
      <LocalOwnerContext.Provider value={{ kind: 'guest', id: 'owner-a' }}>
        <CallTranscriptionSection t={(key, vars) => t('ja', key, vars)} />
      </LocalOwnerContext.Provider>
    </UiLanguageProvider>
  );
  return meeting;
}

afterEach(() => {
  cleanup();
  delete window.electron;
});

it('on a Mac that cannot transcribe, only says where it works', async () => {
  const meeting = renderSection({ supported: false });
  expect(
    await screen.findByText('macOS 26 以降の Apple シリコンの Mac で使えます。')
  ).toBeInTheDocument();
  expect(screen.queryByText(/文字起こしするか確認します/)).toBeNull();
  expect(screen.queryByRole('combobox')).toBeNull();
  expect(meeting.getDefaultCallLists).not.toHaveBeenCalled();
});

it('lists what the recorder listens to by default, and saves an edit as the user’s list', async () => {
  const meeting = renderSection();
  const apps = await screen.findByRole('list', { name: '対象のアプリ' });
  await waitFor(() => expect(apps).toHaveTextContent('Zoom'));
  const sites = screen.getByRole('list', { name: '対象のサイト（Chrome と Safari）' });
  expect(sites).toHaveTextContent('meet.google.com');
  expect(sites).toHaveTextContent('zoom.us');

  fireEvent.click(screen.getByRole('button', { name: 'zoom.us を削除' }));
  await waitFor(() =>
    expect(meeting.updateSettings).toHaveBeenCalledWith({
      ...SETTINGS,
      callSites: ['meet.google.com'],
    })
  );
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
      new Promise<undefined>((resolve) => {
        finishSave = () => resolve(undefined);
      })
  );
  renderSection({ updateSettings });
  await screen.findByRole('list', { name: '対象のサイト（Chrome と Safari）' });

  fireEvent.click(screen.getByRole('button', { name: 'zoom.us を削除' }));
  // The language and the lists wait for that save to finish.
  const language = screen.getByRole('combobox', { name: '言語' });
  expect(language).toBeDisabled();
  fireEvent.change(language, { target: { value: 'ja-JP' } });
  fireEvent.click(screen.getByRole('button', { name: 'meet.google.com を削除' }));
  expect(updateSettings).toHaveBeenCalledTimes(1);

  await act(async () => finishSave());
  fireEvent.change(screen.getByRole('combobox', { name: '言語' }), {
    target: { value: 'ja-JP' },
  });
  expect(updateSettings).toHaveBeenLastCalledWith({
    ...SETTINGS,
    callSites: ['meet.google.com'],
    locale: 'ja-JP',
  });
});

it('keeps the language when the default lists cannot be read, and reads them again on request', async () => {
  const getDefaultCallLists = vi
    .fn<() => Promise<MeetingCallLists>>()
    .mockRejectedValueOnce(new Error('config show failed'))
    .mockResolvedValue(DEFAULTS);
  const meeting = renderSection({ getDefaultCallLists });
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '対象のアプリとサイトを読み込めませんでした。'
  );
  // A failed read is not an empty list.
  expect(screen.queryByRole('list', { name: '対象のアプリ' })).toBeNull();

  fireEvent.change(screen.getByRole('combobox', { name: '言語' }), {
    target: { value: 'en-US' },
  });
  await waitFor(() =>
    expect(meeting.updateSettings).toHaveBeenCalledWith({ ...SETTINGS, locale: 'en-US' })
  );

  fireEvent.click(screen.getByRole('button', { name: 'もう一度読み込む' }));
  expect(
    await screen.findByRole('list', { name: '対象のサイト（Chrome と Safari）' })
  ).toBeVisible();
});

it('offers to resume recording when it did not come back after the app picker', async () => {
  const setCaptureEditing = vi
    .fn<(request: { kind: string }) => Promise<boolean>>()
    .mockImplementation(async () => true)
    .mockImplementationOnce(async () => true)
    .mockImplementationOnce(async () => {
      throw new Error('restart failed');
    });
  renderSection({ setCaptureEditing });
  const add = await screen.findByRole('button', { name: '+ アプリを追加' });

  fireEvent.click(add);
  await screen.findByRole('dialog', { name: 'アプリを選択' });
  fireEvent.keyDown(document, { key: 'Escape' });
  const resume = await screen.findByRole('button', { name: '記録を再開' });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(add).toBeDisabled();

  fireEvent.click(resume);
  await waitFor(() => expect(screen.queryByRole('button', { name: '記録を再開' })).toBeNull());
  expect(add).toBeEnabled();
  expect(setCaptureEditing.mock.calls.map(([request]) => request.kind)).toEqual([
    'begin',
    'end',
    'end',
  ]);
});
