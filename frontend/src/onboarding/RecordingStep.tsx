import { Check } from 'lucide-react';
import { useState } from 'react';

import { useCaptureGate } from '@/hooks/useCaptureGate';
import type { CapturePermissionPane } from '../../electron/src/ipc/schemas/onboarding';
import type { RecordingStartResult } from '../../electron/src/screenshot/screenshotSync';
import { AiConnectionRow as Row } from '../pages/settings/components/AiConnectionRow';
import { RecordingSwitch } from '../pages/settings/components/RecordingSwitch';
import type { Translate } from '../pages/settings/types';
import type { useScreenshotCaptureStatus } from '../pages/settings/useScreenshotCaptureStatus';
import '../pages/settings/settingsPage.css';
import '../pages/settings/components/aiConnection.css';
import './onboardingSteps.css';

const PERMISSIONS = [
  {
    id: 'accessibility',
    label: 'onboarding.recording.accessibility',
    pane: 'onboarding.recording.accessibility',
  },
  {
    id: 'screen_recording',
    label: 'onboarding.recording.screenRecording',
    pane: 'onboarding.recording.screenRecordingPane',
  },
] as const;

/**
 * Setup's recording step: what recording takes, the two macOS permissions it needs, and the
 * switch. The parent owns `capture`, so recording being on (the step being done) is read
 * there from `capture.isCapturingScreenshots`.
 */
export function RecordingStep({
  t,
  capture,
}: {
  t: Translate;
  capture: ReturnType<typeof useScreenshotCaptureStatus>;
}) {
  const { gate } = useCaptureGate();
  // Which panes were opened from here: macOS reports nothing until the user acts there.
  const [requested, setRequested] = useState<ReadonlySet<CapturePermissionPane>>(new Set());
  const [relaunching, setRelaunching] = useState(false);
  const [startOutcome, setStartOutcome] = useState<Exclude<RecordingStartResult, 'started'> | null>(
    null
  );

  const granted: Record<CapturePermissionPane, boolean> = {
    accessibility: gate?.permissions.accessibility ?? false,
    screen_recording: gate?.permissions.screenRecording ?? false,
  };
  const bothGranted = granted.accessibility && granted.screen_recording;
  const { isCapturingScreenshots, captureStatusUnavailable } = capture;

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

  const changeRecording = async (enabled: boolean) => {
    setStartOutcome(null);
    if (!enabled) {
      await capture.setScreenshotsEnabled(false);
      return;
    }
    const result = await capture.startRecording();
    if (result !== 'started') setStartOutcome(result);
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

      <section className="dashboard-section ai-connection onb-section">
        <h3 className="ai-group">{t('onboarding.recording.permissionsTitle')}</h3>
        <p className="ai-note ai-group-note">{t('onboarding.recording.permissionsNote')}</p>
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
                <>
                  <div className="ai-inline">
                    <span className="onb-waiting" role="status">
                      {t('onboarding.recording.waiting')}
                    </span>
                    {/* macOS applies a Screen Recording grant only to a reopened app, so it
                        keeps reading as missing until then. */}
                    {permission.id === 'screen_recording' ? (
                      <button
                        type="button"
                        className="settings-action-button"
                        disabled={relaunching}
                        onClick={() => void relaunch()}
                      >
                        {t('onboarding.recording.relaunch')}
                      </button>
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
                  <p className="ai-note ai-note--control">
                    {t('onboarding.recording.privacyPane')} <span aria-hidden="true">›</span>{' '}
                    {t(permission.pane)}
                  </p>
                  {permission.id === 'screen_recording' ? (
                    <p className="ai-note ai-note--control">
                      {t('onboarding.recording.relaunchNote')}
                    </p>
                  ) : null}
                </>
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

        <div className="ai-rule" />

        <div className="settings-section-header">
          <div className="settings-section-heading">
            <h3 className="ai-group onb-switch-title">{t('settings.screenshotCapture.title')}</h3>
            <div
              className="settings-section-status"
              role={captureStatusUnavailable ? 'alert' : undefined}
            >
              {captureStatusUnavailable
                ? t('settings.screenshotCapture.unavailable')
                : isCapturingScreenshots === null
                  ? t('settings.loadingStatus')
                  : isCapturingScreenshots
                    ? t('common.active')
                    : bothGranted
                      ? t('onboarding.recording.switchReady')
                      : t('onboarding.recording.switchBlocked')}
            </div>
          </div>
          <RecordingSwitch
            isCapturing={isCapturingScreenshots}
            canStart={bothGranted && capture.isScreenshotCaptureAvailable}
            isProcessing={capture.isProcessing}
            onChange={changeRecording}
          />
        </div>
        {/* A start that waited on macOS can still begin later; then its notice is stale. */}
        {startOutcome !== null && !isCapturingScreenshots ? (
          <p className="ai-note onb-start-problem" role="alert">
            {t(
              startOutcome === 'permission_pending'
                ? 'recordingIntro.permissionPending'
                : 'recordingIntro.startFailed'
            )}
          </p>
        ) : null}
        <p className="ai-note">{t('recordingIntro.settingsNote')}</p>
      </section>
    </>
  );
}
