import { Check } from 'lucide-react';
import { type ReactNode, useId, useState } from 'react';

import { useCaptureGate } from '@/hooks/useCaptureGate';
import type { CapturePermissionPane } from '../../electron/src/ipc/schemas/onboarding';
import type { Translate } from '../pages/settings/types';
import '../components/recordingIntro.css';
import '../pages/settings/settingsPage.css';
import './onboardingSteps.css';
import type { useRecordingStep } from './useRecordingStep';

const PERMISSIONS = [
  {
    id: 'accessibility',
    title: 'onboarding.recording.accessibilityTitle',
    waiting: 'onboarding.recording.accessibilityWaiting',
  },
  {
    id: 'screen_recording',
    title: 'onboarding.recording.screenRecordingTitle',
    waiting: 'onboarding.recording.screenRecordingWaiting',
  },
] as const;

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

/** Setup's recording step: what recording takes, as one checklist of the two macOS
 *  permissions and the start. */
export function RecordingStep({
  t,
  recording,
}: {
  t: Translate;
  recording: ReturnType<typeof useRecordingStep>;
}) {
  const { gate } = useCaptureGate();
  // Which permissions were asked for here: macOS reports nothing until the user acts there.
  const [requested, setRequested] = useState<ReadonlySet<CapturePermissionPane>>(new Set());
  const allowId = useId();
  const { capture, startOutcome, isRecording, start } = recording;

  const granted: Record<CapturePermissionPane, boolean> = {
    accessibility: gate?.permissions.accessibility ?? false,
    screen_recording: gate?.permissions.screenRecording ?? false,
  };
  const bothGranted = granted.accessibility && granted.screen_recording;

  const askFor = async (permission: CapturePermissionPane) => {
    setRequested((current) => new Set(current).add(permission));
    try {
      await window.electron?.screenshot?.askForPermission(permission);
    } catch (error) {
      // The row stays waiting with System Settings one press away.
      console.error('Failed to ask for the permission:', error);
    }
  };

  const startProblem = capture.captureStatusUnavailable
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
        {PERMISSIONS.map((permission) => {
          const waiting = requested.has(permission.id);
          const buttonId = `${allowId}-${permission.id}`;
          return (
            <CheckItem
              key={permission.id}
              title={t(permission.title)}
              note={!granted[permission.id] && waiting ? t(permission.waiting) : null}
              done={granted[permission.id]}
              action={(titleId) =>
                granted[permission.id] ? (
                  <span className="onb-check-state">{t('onboarding.recording.granted')}</span>
                ) : (
                  // One button in both states, so focus stays on it after the first press.
                  <button
                    type="button"
                    id={buttonId}
                    className={waiting ? 'settings-text-button' : 'settings-action-button'}
                    aria-labelledby={`${buttonId} ${titleId}`}
                    onClick={() => void askFor(permission.id)}
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
                  !bothGranted ||
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
