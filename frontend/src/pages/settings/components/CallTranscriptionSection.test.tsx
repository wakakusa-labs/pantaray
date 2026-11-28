import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { t } from '@/i18n/translate';
import type { MeetingSettings } from '../../../../electron/src/context/meetingTranscript';
import { CallTranscriptionSection } from './CallTranscriptionSection';

const SETTINGS: MeetingSettings = { enabled: true, callApps: null, callSites: null, locale: null };
const DEFAULTS = { callApps: ['us.zoom.xos'], callSites: ['meet.google.com', 'zoom.us'] };

function renderSection(supported: boolean) {
  const meeting = {
    getSettings: vi.fn(async () => ({ settings: SETTINGS, supported })),
    getDefaultCallLists: vi.fn(async () => DEFAULTS),
    getAvailability: vi.fn(async () => ({ action_required: ['audio_capture'] })),
    updateSettings: vi.fn(async () => undefined),
    prepare: vi.fn(async () => 'requested'),
  };
  window.electron = {
    meeting,
    privacy: {
      listInstalledApps: vi.fn(async () => [
        { name: 'Zoom', bundleId: 'us.zoom.xos', iconDataUrl: null },
      ]),
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

it('on a Mac that cannot transcribe, says where it works and offers no switch', async () => {
  const meeting = renderSection(false);
  expect(
    await screen.findByText('macOS 26 以降の Apple シリコンの Mac で使えます。')
  ).toBeInTheDocument();
  expect(screen.queryByRole('switch')).toBeNull();
  expect(meeting.getDefaultCallLists).not.toHaveBeenCalled();
});

it('lists what the recorder listens to by default, and saves an edit as the user’s list', async () => {
  const meeting = renderSection(true);
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
  renderSection(true);
  expect(await screen.findByText('システムオーディオ録音')).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: /システム設定を開く システムオーディオ録音/ })
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: /システム設定を開く マイク/ })).toBeNull();
});
