import { Globe, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { CheckItem } from '@/components/Checklist';
import type { MessageKey } from '@/i18n/types';
import type {
  MeetingAvailability,
  MeetingCallLists,
  MeetingPermission,
  MeetingSettings,
} from '../../../../electron/src/context/meetingTranscript';
import { findInstalledApp, parseWebsiteHost } from '../model';
import type { InstalledApp, Translate } from '../types';
import { useCaptureEditingSession } from '../useCaptureEditingSession';
import { AppPickerPopover } from './AppPickerPopover';
import './callTranscription.css';
import './recordingFilter.css';

type Loaded = { settings: MeetingSettings; defaults: MeetingCallLists };

const PERMISSIONS = [
  ['microphone', 'settings.callTranscription.microphone'],
  ['audio_capture', 'settings.callTranscription.systemAudio'],
] as const satisfies readonly (readonly [MeetingPermission, MessageKey])[];

/** Speech locales offered, named in their own language; null follows the Mac. */
const LOCALES = [
  ['ja-JP', '日本語'],
  ['en-US', 'English'],
] as const;

/**
 * Call transcription: off until the user turns it on, and then asked about call by call. The
 * apps and sites start as the recorder's own lists; editing one saves it as the user's.
 */
export function CallTranscriptionSection({ t }: { t: Translate }) {
  const meeting = window.electron?.meeting;
  const [loaded, setLoaded] = useState<Loaded | 'loading' | 'unsupported' | 'failed'>('loading');
  const [availability, setAvailability] = useState<MeetingAvailability | null>(null);
  const [error, setError] = useState<MessageKey | null>(null);

  const readAvailability = useCallback(() => {
    meeting?.getAvailability().then(setAvailability, (reason: unknown) => {
      console.error('Failed to read call transcription availability:', reason);
    });
  }, [meeting]);

  useEffect(() => {
    if (!meeting) return;
    (async () => {
      const { settings, supported } = await meeting.getSettings();
      if (!supported) return setLoaded('unsupported');
      setLoaded({ settings, defaults: await meeting.getDefaultCallLists() });
      readAvailability();
    })().catch((reason: unknown) => {
      console.error('Failed to load call transcription settings:', reason);
      setLoaded('failed');
    });
    // Permissions are granted in System Settings, so coming back here reads them again.
    window.addEventListener('focus', readAvailability);
    return () => window.removeEventListener('focus', readAvailability);
  }, [meeting, readAvailability]);

  if (!meeting || loaded === 'loading') return null;

  const header = (control: React.ReactNode) => (
    <div className="settings-section-header">
      <div className="settings-section-heading">
        <h4 className="dashboard-subsection-title">{t('settings.callTranscription.title')}</h4>
      </div>
      {control}
    </div>
  );
  const description = (
    <p className="dashboard-section-description">{t('settings.callTranscription.description')}</p>
  );
  if (loaded === 'unsupported' || loaded === 'failed') {
    return (
      <div className="dashboard-subsection">
        {header(null)}
        {description}
        <p
          className="dashboard-section-description"
          role={loaded === 'failed' ? 'alert' : undefined}
        >
          {t(
            loaded === 'failed'
              ? 'settings.callTranscription.loadFailed'
              : 'settings.callTranscription.unsupported'
          )}
        </p>
      </div>
    );
  }

  const { settings, defaults } = loaded;
  // `prepare` asks macOS for the two permissions once and fetches the language's speech data.
  const save = async (next: MeetingSettings, prepare = false) => {
    try {
      setError(null);
      await meeting.updateSettings(next);
      setLoaded({ settings: next, defaults });
      if (prepare && next.enabled) await meeting.prepare();
    } catch (reason) {
      console.error('Failed to save call transcription settings:', reason);
      setError('settings.callTranscription.saveFailed');
    }
    readAvailability();
  };
  const missing =
    typeof availability === 'object' && availability && 'action_required' in availability
      ? availability.action_required
      : [];
  const unavailable =
    typeof availability === 'object' && availability && 'unavailable' in availability
      ? availability.unavailable
      : null;

  return (
    <div className="dashboard-subsection">
      {header(
        <button
          type="button"
          role="switch"
          aria-checked={settings.enabled}
          aria-label={t('settings.callTranscription.title')}
          className={settings.enabled ? 'settings-toggle settings-toggle--on' : 'settings-toggle'}
          onClick={() => void save({ ...settings, enabled: !settings.enabled }, true)}
        >
          <span className="settings-toggle-thumb" />
          <span className="settings-toggle-label">
            {t(settings.enabled ? 'common.on' : 'common.off')}
          </span>
        </button>
      )}
      {description}
      {error ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t(error)}
        </p>
      ) : null}
      {/* The recorder reports the permissions only while it can transcribe or waits on them. */}
      {settings.enabled && (availability === 'available' || missing.length > 0) ? (
        <ul className="checklist">
          {PERMISSIONS.map(([permission, title]) => {
            const granted = !missing.includes(permission);
            return (
              <CheckItem
                key={permission}
                title={t(title)}
                status={granted ? 'done' : 'pending'}
                action={(titleId) =>
                  granted ? (
                    <span className="check-item-state">{t('onboarding.recording.granted')}</span>
                  ) : (
                    <button
                      type="button"
                      id={`call-transcription-${permission}`}
                      className="settings-action-button"
                      aria-labelledby={`call-transcription-${permission} ${titleId}`}
                      onClick={() =>
                        void window.electron?.screenshot
                          ?.openPermissionSettings(permission)
                          .catch((reason: unknown) =>
                            console.error('Failed to open System Settings:', reason)
                          )
                      }
                    >
                      {t('recordingIntro.openSettings')}
                    </button>
                  )
                }
              />
            );
          })}
        </ul>
      ) : null}
      {settings.enabled && missing.includes('speech_model') ? (
        <p className="dashboard-section-description">
          {t('settings.callTranscription.preparingLanguage')}
        </p>
      ) : null}
      {settings.enabled && unavailable === 'locale_unsupported' ? (
        <p className="dashboard-section-description">
          {t('settings.callTranscription.languageUnsupported')}
        </p>
      ) : null}

      <label className="call-transcription-field">
        <span className="call-transcription-label">{t('settings.callTranscription.language')}</span>
        <select
          className="recording-filter-mode call-transcription-select"
          value={settings.locale ?? ''}
          onChange={(event) => void save({ ...settings, locale: event.target.value || null }, true)}
        >
          <option value="">{t('settings.callTranscription.languageAuto')}</option>
          {LOCALES.map(([locale, name]) => (
            <option key={locale} value={locale}>
              {name}
            </option>
          ))}
        </select>
      </label>

      <p className="dashboard-section-description call-transcription-lists-note">
        {t('settings.callTranscription.listsNote')}
      </p>
      <CallAppList
        apps={settings.callApps ?? defaults.callApps}
        t={t}
        onChange={(next) => void save({ ...settings, callApps: next })}
        onError={setError}
      />
      <CallSiteList
        sites={settings.callSites ?? defaults.callSites}
        t={t}
        onChange={(next) => void save({ ...settings, callSites: next })}
      />
    </div>
  );
}

function RemovableRow({
  icon,
  name,
  t,
  onRemove,
}: {
  icon: React.ReactNode;
  name: string;
  t: Translate;
  onRemove: () => void;
}) {
  return (
    <li className="recording-filter-row">
      {icon}
      <span className="recording-filter-row-text">
        <span className="recording-filter-row-name">{name}</span>
      </span>
      <button
        type="button"
        className="recording-filter-remove"
        aria-label={t('settings.recordingFilter.removeEntry', { name })}
        onClick={onRemove}
      >
        <X size={14} aria-hidden="true" />
      </button>
    </li>
  );
}

/**
 * The call apps, by bundle identifier, shown by their installed name. The picker lists every
 * installed app, so it opens only behind the recording pause, as the recording filter's does.
 */
function CallAppList({
  apps,
  t,
  onChange,
  onError,
}: {
  apps: string[];
  t: Translate;
  onChange: (apps: string[]) => void;
  onError: (error: MessageKey) => void;
}) {
  const [installed, setInstalled] = useState<InstalledApp[]>([]);
  const [isPickerOpen, setPickerOpen] = useState(false);
  const addRef = useRef<HTMLButtonElement>(null);
  const editing = useCaptureEditingSession();
  const listInstalledApps = window.electron?.privacy?.listInstalledApps;

  useEffect(() => {
    listInstalledApps?.().then(setInstalled, (reason: unknown) =>
      console.error('Failed to list installed apps:', reason)
    );
  }, [listInstalledApps]);

  const entries = apps.map((bundleId) => ({
    bundleId,
    installed: findInstalledApp(installed, { name: bundleId, bundleId }),
  }));
  const openPicker = async () => {
    try {
      if (await editing.open()) setPickerOpen(true);
    } catch {
      onError('settings.recordingFilter.pauseFailed');
    }
  };
  const closePicker = async () => {
    setPickerOpen(false);
    try {
      await editing.close();
    } catch {
      onError('settings.recordingFilter.resumeFailed');
    }
  };

  return (
    <section className="call-transcription-list-section">
      <h5 className="call-transcription-label">{t('settings.callTranscription.apps')}</h5>
      <ul
        className="recording-filter-list call-transcription-list"
        aria-label={t('settings.callTranscription.apps')}
      >
        {entries.map(({ bundleId, installed: app }) => (
          <RemovableRow
            key={bundleId}
            icon={
              app?.iconDataUrl ? (
                <img className="recording-filter-row-icon" src={app.iconDataUrl} alt="" />
              ) : (
                <span className="recording-filter-row-icon" aria-hidden="true" />
              )
            }
            name={app?.name ?? bundleId}
            t={t}
            onRemove={() => onChange(apps.filter((listed) => listed !== bundleId))}
          />
        ))}
      </ul>
      <div className="recording-filter-add">
        <button
          ref={addRef}
          type="button"
          className="settings-action-button"
          aria-expanded={isPickerOpen}
          onClick={() => void (isPickerOpen ? closePicker() : openPicker())}
        >
          {t('settings.recordingFilter.apps.add')}
        </button>
        {isPickerOpen ? (
          <AppPickerPopover
            installedApps={installed.filter((app) => app.bundleId !== null)}
            isLoading={false}
            listed={apps.map((bundleId) => ({ name: bundleId, bundleId }))}
            loadError={null}
            triggerRef={addRef}
            t={t}
            onDismiss={() => void closePicker()}
            onSelect={(app) => app.bundleId && onChange([...apps, app.bundleId])}
          />
        ) : null}
      </div>
    </section>
  );
}

/** The meeting sites, matched in Chrome and Safari; a site covers its subdomains. */
function CallSiteList({
  sites,
  t,
  onChange,
}: {
  sites: string[];
  t: Translate;
  onChange: (sites: string[]) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const add = () => {
    const parsed = parseWebsiteHost(draft ?? '');
    if (!parsed.ok) return setInvalid(true);
    if (!sites.includes(parsed.host)) onChange([...sites, parsed.host]);
    setDraft(null);
  };

  return (
    <section className="call-transcription-list-section">
      <h5 className="call-transcription-label">{t('settings.callTranscription.sites')}</h5>
      <ul
        className="recording-filter-list call-transcription-list"
        aria-label={t('settings.callTranscription.sites')}
      >
        {sites.map((site) => (
          <RemovableRow
            key={site}
            icon={<Globe className="recording-filter-row-icon" size={18} aria-hidden="true" />}
            name={site}
            t={t}
            onRemove={() => onChange(sites.filter((listed) => listed !== site))}
          />
        ))}
      </ul>
      {draft === null ? (
        <div className="recording-filter-add">
          <button type="button" className="settings-action-button" onClick={() => setDraft('')}>
            {t('settings.recordingFilter.websites.add')}
          </button>
        </div>
      ) : (
        <form
          className="recording-filter-host-form"
          onSubmit={(event) => {
            event.preventDefault();
            add();
          }}
        >
          <input
            autoFocus
            className="recording-filter-host-input"
            placeholder="meet.example.com"
            aria-label={t('settings.recordingFilter.websites.inputLabel')}
            aria-invalid={invalid}
            aria-describedby={invalid ? 'call-transcription-site-error' : undefined}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setInvalid(false);
            }}
          />
          <button type="submit" className="settings-action-button" disabled={!draft.trim()}>
            {t('common.save')}
          </button>
        </form>
      )}
      {invalid ? (
        <p id="call-transcription-site-error" className="recording-filter-empty" role="alert">
          {t('settings.recordingFilter.websites.invalidHost')}
        </p>
      ) : null}
    </section>
  );
}
