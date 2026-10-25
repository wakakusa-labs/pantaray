import { Check } from 'lucide-react';
import { type ReactNode, useEffect, useId, useState } from 'react';

import type {
  CapturePermission,
  CapturePermissionCheck,
} from '../../electron/src/context/zaneiProcess';
import type { Translate } from '../pages/settings/types';
import '../components/recordingIntro.css';
import '../pages/settings/settingsPage.css';
import './onboardingSteps.css';
import type { useRecordingStep } from './useRecordingStep';

const ROW_COPY = {
  read_accessibility_tree: {
    title: 'onboarding.recording.accessibilityTitle',
    waiting: 'onboarding.recording.accessibilityWaiting',
  },
  observe_input: {
    title: 'onboarding.recording.inputMonitoringTitle',
    waiting: 'onboarding.recording.inputMonitoringWaiting',
  },
  capture_screen: {
    title: 'onboarding.recording.screenRecordingTitle',
    waiting: 'onboarding.recording.screenRecordingWaiting',
  },
  automate_browser: {
    title: 'onboarding.recording.chromeTitle',
    waiting: 'onboarding.recording.chromeWaiting',
  },
  automate_safari: {
    title: 'onboarding.recording.safariTitle',
    waiting: 'onboarding.recording.safariWaiting',
  },
} as const satisfies Record<CapturePermission, { title: string; waiting: string }>;

/**
 * What a start would wait on, as main reads it for that start. Read again whenever the
 * window regains focus, which is how the user comes back from System Settings.
 */
function useStartPermissions(): CapturePermissionCheck[] | 'unavailable' | null {
  const [checks, setChecks] = useState<CapturePermissionCheck[] | 'unavailable' | null>(null);
  useEffect(() => {
    let active = true;
    let latest = 0;
    const read = () => {
      const call = ++latest;
      window.electron?.screenshot?.getPermissions().then(
        (next) => {
          if (active && call === latest) setChecks(next);
        },
        (error: unknown) => {
          console.error('Failed to read the recording permissions:', error);
          if (active && call === latest) setChecks('unavailable');
        }
      );
    };
    read();
    window.addEventListener('focus', read);
    return () => {
      active = false;
      window.removeEventListener('focus', read);
    };
  }, []);
  return checks;
}

/** One checklist row: its mark, its title with at most one note, and its one action. */
function CheckItem({
  title,
  note,
  problem = false,
  done,
  action,
}: {
  title: string;
  note: string | null;
  /** The note says why the row is stuck, so it is drawn as a warning and announced. */
  problem?: boolean;
  done: boolean;
  action: (titleId: string) => ReactNode;
}) {
  const titleId = useId();
  return (
    <li className={done ? 'onb-check onb-check--done' : 'onb-check'}>
      <span className="onb-check-mark" aria-hidden="true">
        {done ? <Check size={13} strokeWidth={2.4} /> : null}
      </span>
      <span className="onb-check-text">
        <span className="onb-check-title" id={titleId}>
          {title}
        </span>
        {note ? (
          <span
            className={problem ? 'onb-check-note onb-check-note--problem' : 'onb-check-note'}
            role={problem ? 'alert' : undefined}
          >
            {note}
          </span>
        ) : null}
      </span>
      <span className="onb-check-action">{action(titleId)}</span>
    </li>
  );
}

/** Setup's recording step: what recording takes, as one checklist of the macOS permissions
 *  a start waits on, and the start. */
export function RecordingStep({
  t,
  recording,
}: {
  t: Translate;
  recording: ReturnType<typeof useRecordingStep>;
}) {
  const checks = useStartPermissions();
  // Which permissions were asked for here: macOS reports nothing until the user acts there.
  const [requested, setRequested] = useState<ReadonlySet<CapturePermission>>(new Set());
  const allowId = useId();
  const { capture, startOutcome, isRecording, start } = recording;
  const allGranted = Array.isArray(checks) && checks.every((check) => check.granted);

  const askFor = async (permission: CapturePermission) => {
    setRequested((current) => new Set(current).add(permission));
    try {
      await window.electron?.screenshot?.askForPermission(permission);
    } catch (error) {
      // The row stays waiting with System Settings one press away.
      console.error('Failed to ask for the permission:', error);
    }
  };

  // Every permission a start waits on is a row above, so a start that did not finish is
  // unexpected whatever main answered.
  const startProblem =
    capture.captureStatusUnavailable || checks === 'unavailable'
      ? t('settings.screenshotCapture.unavailable')
      : startOutcome !== null
        ? t('recordingIntro.startFailed')
        : null;

  return (
    <>
      <p className="onb-lead">{t('onboarding.recording.lead')}</p>
      <details className="onb-notes">
        <summary>{t('onboarding.recording.details')}</summary>
        <p>{t('recordingIntro.captured')}</p>
        <p>{t('recordingIntro.browsers')}</p>
        <p>{t('recordingIntro.excluded')}</p>
      </details>

      <ul className="onb-checklist">
        {(Array.isArray(checks) ? checks : []).map(({ permission, granted }) => {
          const waiting = requested.has(permission);
          const buttonId = `${allowId}-${permission}`;
          return (
            <CheckItem
              key={permission}
              title={t(ROW_COPY[permission].title)}
              note={!granted && waiting ? t(ROW_COPY[permission].waiting) : null}
              done={granted}
              action={(titleId) =>
                granted ? (
                  <span className="onb-check-state">{t('onboarding.recording.granted')}</span>
                ) : (
                  // One button in both states, so focus stays on it after the first press.
                  <button
                    type="button"
                    id={buttonId}
                    className={waiting ? 'settings-text-button' : 'settings-action-button'}
                    aria-labelledby={`${buttonId} ${titleId}`}
                    onClick={() => void askFor(permission)}
                  >
                    {t(waiting ? 'recordingIntro.openSettings' : 'onboarding.recording.allow')}
                  </button>
                )
              }
            />
          );
        })}
        <CheckItem
          title={t('onboarding.recording.startTitle')}
          note={startProblem ?? t('onboarding.recording.startNote')}
          problem={startProblem !== null}
          done={isRecording === true}
          action={() =>
            isRecording ? (
              <span className="onb-check-state">{t('onboarding.recording.recording')}</span>
            ) : (
              <button
                type="button"
                className="recording-intro-primary"
                disabled={
                  !allGranted ||
                  isRecording === null ||
                  !capture.isScreenshotCaptureAvailable ||
                  capture.isProcessing
                }
                onClick={() => void start()}
              >
                {t('onboarding.recording.start')}
              </button>
            )
          }
        />
      </ul>
    </>
  );
}
