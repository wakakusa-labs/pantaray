import { Check } from 'lucide-react';
import { useState } from 'react';

import { useCaptureGate } from '@/hooks/useCaptureGate';
import type { CapturePermissionPane } from '../../electron/src/ipc/schemas/onboarding';
import { AiConnectionRow as Row } from '../pages/settings/components/AiConnectionRow';
import { RecordingSwitch } from '../pages/settings/components/RecordingSwitch';
import type { Translate } from '../pages/settings/types';
import '../pages/settings/settingsPage.css';
import '../pages/settings/components/aiConnection.css';
import './onboardingSteps.css';
import type { useRecordingStep } from './useRecordingStep';

const PERMISSIONS = [
  {
    id: 'accessibility',
    label: 'onboarding.recording.accessibility',
  },
  {
    id: 'screen_recording',
    label: 'onboarding.recording.screenRecording',
  },
] as const;

/** Setup's recording step: what recording takes, the two macOS permissions it needs, and the switch. */
export function RecordingStep({
  t,
  recording,
}: {
  t: Translate;
  recording: ReturnType<typeof useRecordingStep>;
}) {
  const { gate } = useCaptureGate();
  // Which panes were opened from here: macOS reports nothing until the user acts there.
  const [requested, setRequested] = useState<ReadonlySet<CapturePermissionPane>>(new Set());
  const [relaunching, setRelaunching] = useState(false);
  const { capture, startOutcome, isRecording, changeRecording } = recording;

  const granted: Record<CapturePermissionPane, boolean> = {
    accessibility: gate?.permissions.accessibility ?? false,
    screen_recording: gate?.permissions.screenRecording ?? false,
  };
  const bothGranted = granted.accessibility && granted.screen_recording;
  const { captureStatusUnavailable } = capture;

  const openPane = async (permission: CapturePermissionPane) => {
    try {
      await window.electron?.screenshot?.openPermissionSettings(permission);
    } catch (error) {
      // The waiting row still names the pane, so the user can open it by hand.
      console.error('Failed to open System Settings:', error);
    }
  };

  const relaunch = async () => {
    // Every app.relaunch() call starts another instance after this one quits.
    setRelaunching(true);
    try {
      await window.electron?.app?.relaunch();
    } catch (error) {
      console.error('Failed to reopen Pantaray:', error);
      setRelaunching(false);
    }
  };

  return (
    <>
      <p className="onb-lead">{t('onboarding.recording.lead')}</p>
      <details className="onb-notes">
        <summary>{t('onboarding.recording.details')}</summary>
        <p>{t('recordingIntro.captured')}</p>
        <p>{t('recordingIntro.browsers')}</p>
        <p>{t('recordingIntro.excluded')}</p>
      </details>

      <ol className="onb-guide onb-rec-stages">
        <li>
          <h3 className="onb-guide-title">{t('onboarding.recording.permissionsTitle')}</h3>
          <span className="ai-note">{t('onboarding.recording.permissionsNote')}</span>
          <div className="ai-connection onb-rec-rows">
            {PERMISSIONS.map((permission) => {
              const label = t(permission.label);
              return (
                <Row key={permission.id} label={label}>
                  {granted[permission.id] ? (
                    <div className="ai-inline">
                      <span className="onb-granted">
                        <Check size={14} strokeWidth={2.2} aria-hidden="true" />
                        {t('onboarding.recording.granted')}
                      </span>
                    </div>
                  ) : requested.has(permission.id) ? (
                    <div className="ai-inline">
                      {/* macOS applies a Screen Recording grant only to a reopened app, so it
                          keeps reading as missing until then. */}
                      {permission.id === 'screen_recording' ? (
                        <>
                          <span className="onb-waiting" role="status">
                            {t('onboarding.recording.relaunchNote')}
                          </span>
                          <button
                            type="button"
                            className="settings-action-button"
                            disabled={relaunching}
                            onClick={() => void relaunch()}
                          >
                            {t('onboarding.recording.relaunch')}
                          </button>
                        </>
                      ) : null}
                      <button
                        type="button"
                        className="settings-text-button"
                        aria-label={`${label}: ${t('recordingIntro.openSettings')}`}
                        onClick={() => void openPane(permission.id)}
                      >
                        {t('recordingIntro.openSettings')}
                      </button>
                    </div>
                  ) : (
                    <div className="ai-inline">
                      <button
                        type="button"
                        className="settings-action-button"
                        aria-label={`${label}: ${t('onboarding.recording.allow')}`}
                        onClick={() => {
                          setRequested((current) => new Set(current).add(permission.id));
                          void openPane(permission.id);
                        }}
                      >
                        {t('onboarding.recording.allow')}
                      </button>
                    </div>
                  )}
                </Row>
              );
            })}
          </div>
        </li>
        <li>
          <h3 className="onb-guide-title">{t('onboarding.recording.switchTitle')}</h3>
          <span className="ai-note" role={captureStatusUnavailable ? 'alert' : undefined}>
            {captureStatusUnavailable
              ? t('settings.screenshotCapture.unavailable')
              : isRecording === null
                ? t('settings.loadingStatus')
                : isRecording
                  ? t('onboarding.recording.recordingOn')
                  : bothGranted
                    ? t('onboarding.recording.switchReady')
                    : t('onboarding.recording.switchBlocked')}
          </span>
          <div className="ai-connection onb-rec-rows">
            <Row label={t('onboarding.recording.switchLabel')}>
              <div className="ai-inline">
                <RecordingSwitch
                  isCapturing={isRecording}
                  canStart={bothGranted && capture.isScreenshotCaptureAvailable}
                  isProcessing={capture.isProcessing}
                  onChange={changeRecording}
                />
              </div>
            </Row>
          </div>
          {startOutcome !== null ? (
            <p className="ai-note onb-start-problem" role="alert">
              {t(
                startOutcome === 'permission_pending'
                  ? 'onboarding.recording.permissionPending'
                  : 'recordingIntro.startFailed'
              )}
            </p>
          ) : null}
          <span className="ai-note">{t('recordingIntro.settingsNote')}</span>
        </li>
      </ol>
    </>
  );
}
