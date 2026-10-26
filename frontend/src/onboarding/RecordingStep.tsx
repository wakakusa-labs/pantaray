import type { CapturePermission } from '../../electron/src/context/zaneiProcess';
import { CheckItem } from '../components/Checklist';
import { RecordingChecklist } from '../components/RecordingChecklist';
import { useStartPermissions } from '../components/useStartPermissions';
import type { MessageKey } from '../i18n/types';
import type { Translate } from '../pages/settings/types';
import '../components/recordingIntro.css';
import './onboardingSteps.css';
import type { useRecordingStep } from './useRecordingStep';

const TITLES = {
  read_accessibility_tree: 'onboarding.recording.accessibilityTitle',
  observe_input: 'onboarding.recording.inputMonitoringTitle',
  capture_screen: 'onboarding.recording.screenRecordingTitle',
  automate_browser: 'onboarding.recording.chromeTitle',
  automate_safari: 'onboarding.recording.safariTitle',
} as const satisfies Record<CapturePermission, MessageKey>;

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
  const { capture, startOutcome, isRecording, start } = recording;
  const allGranted = Array.isArray(checks) && checks.every((check) => check.granted);

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

      <RecordingChecklist t={t} checks={Array.isArray(checks) ? checks : []} titles={TITLES}>
        <CheckItem
          title={t('onboarding.recording.startTitle')}
          problem={startProblem}
          note={startProblem === null ? t('onboarding.recording.startNote') : null}
          status={isRecording === true ? 'done' : 'pending'}
          action={() =>
            isRecording ? (
              <span className="check-item-state">{t('onboarding.recording.recording')}</span>
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
      </RecordingChecklist>
    </>
  );
}
