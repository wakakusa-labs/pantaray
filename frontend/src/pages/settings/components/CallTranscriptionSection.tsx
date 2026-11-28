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

/** The recorder's own lists, read apart from the settings so a failed read keeps the switch. */
type Defaults = MeetingCallLists | 'loading' | 'failed';

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
 * Call transcription has no switch of its own: macOS's two permissions and the consent asked for
 * each call decide. The apps and sites start as the recorder's own lists; editing one saves it as
 * the user's.
 */
export function CallTranscriptionSection({ t }: { t: Translate }) {
  const meeting = window.electron?.meeting;
  const [loaded, setLoaded] = useState<
    { settings: MeetingSettings } | 'loading' | 'unsupported' | 'failed'
  >('loading');
  const [defaults, setDefaults] = useState<Defaults>('loading');
  const [availability, setAvailability] = useState<MeetingAvailability | null>(null);
  const [error, setError] = useState<MessageKey | null>(null);
  // The last change was saved, but recording did not come back after it.
  const [restartFailed, setRestartFailed] = useState(false);
  // One save at a time: each sends the whole settings, so an edit made while another is saved
  // would send what was on screen before it and undo it.
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);

  const readDefaults = useCallback(() => {
    setDefaults('loading');
    meeting?.getDefaultCallLists().then(setDefaults, (reason: unknown) => {
      console.error('Failed to read the default call lists:', reason);
      setDefaults('failed');
    });
  }, [meeting]);

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
      setLoaded({ settings });
      readDefaults();
      readAvailability();
    })().catch((reason: unknown) => {
      console.error('Failed to load call transcription settings:', reason);
      setLoaded('failed');
    });
    // Permissions are granted in System Settings, so coming back here reads them again.
    window.addEventListener('focus', readAvailability);
    return () => window.removeEventListener('focus', readAvailability);
  }, [meeting, readAvailability, readDefaults]);

  if (!meeting || loaded === 'loading') return null;

  const title = (
    <h4 className="dashboard-subsection-title">{t('settings.callTranscription.title')}</h4>
  );
  if (loaded === 'unsupported' || loaded === 'failed') {
    return (
      <div className="dashboard-subsection">
        {title}
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

  const { settings } = loaded;
  // `prepare` fetches the speech data of the language just chosen.
  const save = async (next: MeetingSettings, prepare = false) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      const result = await meeting.updateSettings(next);
      // Saved either way: the screen shows what is saved, so the next edit builds on it.
      setLoaded({ settings: next });
      setRestartFailed(result === 'restart_failed');
    } catch (reason) {
      console.error('Failed to save call transcription settings:', reason);
      setError('settings.callTranscription.saveFailed');
      return;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
    if (prepare) {
      await meeting.prepare().catch((reason: unknown) => {
        console.error('Failed to prepare call transcription:', reason);
      });
    }
    readAvailability();
  };
  // A list the user has not set is the recorder's; until that is read, it cannot be edited.
  const listed = (own: string[] | null, pick: (lists: MeetingCallLists) => string[]) =>
    own ?? (typeof defaults === 'object' ? pick(defaults) : null);
  const callApps = listed(settings.callApps, (lists) => lists.callApps);
  const callSites = listed(settings.callSites, (lists) => lists.callSites);
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
      {title}
      <p className="dashboard-section-description">{t('settings.callTranscription.description')}</p>
      {restartFailed ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t('settings.callTranscription.resumeFailed')}{' '}
          <button
            type="button"
            className="settings-text-button"
            disabled={saving}
            // Sending the saved settings again brings recording back the same way.
            onClick={() => void save(settings)}
          >
            {t('settings.callTranscription.resume')}
          </button>
        </p>
      ) : null}
      {error ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t(error)}
        </p>
      ) : null}
      {/* The recorder reports the permissions only while it can transcribe or waits on them. */}
      {availability === 'available' || missing.length > 0 ? (
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
      {missing.includes('speech_model') ? (
        <p className="dashboard-section-description">
          {t('settings.callTranscription.preparingLanguage')}
        </p>
      ) : null}
      {unavailable === 'locale_unsupported' ? (
        <p className="dashboard-section-description">
          {t('settings.callTranscription.languageUnsupported')}
        </p>
      ) : null}

      <label className="call-transcription-field">
        <span className="call-transcription-label">{t('settings.callTranscription.language')}</span>
        <select
          className="recording-filter-mode call-transcription-select"
          value={settings.locale ?? ''}
          disabled={saving}
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
      {(callApps === null || callSites === null) && defaults === 'failed' ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t('settings.callTranscription.listsLoadFailed')}{' '}
          <button type="button" className="settings-text-button" onClick={readDefaults}>
            {t('settings.callTranscription.reload')}
          </button>
        </p>
      ) : null}
      {callApps !== null ? (
        <CallAppList
          apps={callApps}
          disabled={saving}
          t={t}
          onChange={(next) => void save({ ...settings, callApps: next })}
          onError={setError}
        />
      ) : null}
      {callSites !== null ? (
        <CallSiteList
          sites={callSites}
          disabled={saving}
          t={t}
          onChange={(next) => void save({ ...settings, callSites: next })}
        />
      ) : null}
    </div>
  );
}

function RemovableRow({
  icon,
  name,
  disabled,
  t,
  onRemove,
}: {
  icon: React.ReactNode;
  name: string;
  disabled: boolean;
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
        disabled={disabled}
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
  disabled,
  t,
  onChange,
  onError,
}: {
  apps: string[];
  disabled: boolean;
  t: Translate;
  onChange: (apps: string[]) => void;
  onError: (error: MessageKey) => void;
}) {
  const [installed, setInstalled] = useState<InstalledApp[]>([]);
  const [isPickerOpen, setPickerOpen] = useState(false);
  // The picker is closed but recording did not come back; only resuming it again is offered.
  const [resumeFailed, setResumeFailed] = useState(false);
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
      setResumeFailed(false);
    } catch (reason) {
      console.error('Failed to resume recording after the app picker:', reason);
      setResumeFailed(true);
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
            disabled={disabled}
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
          disabled={disabled || resumeFailed}
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
      {resumeFailed ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t('settings.callTranscription.resumeFailed')}{' '}
          <button type="button" className="settings-text-button" onClick={() => void closePicker()}>
            {t('settings.callTranscription.resume')}
          </button>
        </p>
      ) : null}
    </section>
  );
}

/** The meeting sites, matched in Chrome and Safari; a site covers its subdomains. */
function CallSiteList({
  sites,
  disabled,
  t,
  onChange,
}: {
  sites: string[];
  disabled: boolean;
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
            disabled={disabled}
            t={t}
            onRemove={() => onChange(sites.filter((listed) => listed !== site))}
          />
        ))}
      </ul>
      {draft === null ? (
        <div className="recording-filter-add">
          <button
            type="button"
            className="settings-action-button"
            disabled={disabled}
            onClick={() => setDraft('')}
          >
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
          <button
            type="submit"
            className="settings-action-button"
            disabled={disabled || !draft.trim()}
          >
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
