import { useCallback, useEffect, useRef, useState } from 'react';

import { CheckItem } from '@/components/Checklist';
import type { MessageKey } from '@/i18n/types';
import type {
  MeetingAvailability,
  MeetingPermission,
  MeetingSettings,
} from '../../../../electron/src/context/meetingTranscript';
import type { Translate } from '../types';
import './callTranscription.css';
import './recordingFilter.css';

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
 * each call decide.
 */
export function CallTranscriptionSection({ t }: { t: Translate }) {
  const meeting = window.electron?.meeting;
  const [loaded, setLoaded] = useState<
    { settings: MeetingSettings } | 'loading' | 'unsupported' | 'failed'
  >('loading');
  const [availability, setAvailability] = useState<MeetingAvailability | null>(null);
  const [error, setError] = useState<MessageKey | null>(null);
  // The last change was saved, but recording did not come back after it.
  const [restartFailed, setRestartFailed] = useState(false);
  // One save at a time: each sends the whole settings, so an edit made while another is saved
  // would send what was on screen before it and undo it.
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);

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
    </div>
  );
}
