import { Settings2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { useI18n } from '@/context/useI18n';
import { RecordingSwitch } from '@/pages/settings/components/RecordingSwitch';
import type { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';

/** The recording switch, its status and the way to the recording filter. */
export function RecordingControls({
  capture,
  onOpenSettings,
}: {
  capture: ReturnType<typeof useScreenshotCaptureStatus>;
  /** Runs before the settings page opens, so its container can close. */
  onOpenSettings: () => void;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const {
    isCapturingScreenshots,
    captureStatusUnavailable,
    isProcessing,
    isScreenshotCaptureAvailable,
    setScreenshotsEnabled,
  } = capture;

  return (
    <>
      <div className="app-recording-row">
        <div className="app-recording-title">{t('settings.screenshotCapture.title')}</div>
        <RecordingSwitch
          compact
          isCapturing={isCapturingScreenshots}
          canStart={isScreenshotCaptureAvailable}
          isProcessing={isProcessing}
          onChange={setScreenshotsEnabled}
        />
      </div>
      <div className="app-recording-status" role={captureStatusUnavailable ? 'alert' : undefined}>
        {captureStatusUnavailable
          ? t('settings.screenshotCapture.unavailable')
          : isCapturingScreenshots === null
            ? t('settings.loadingStatus')
            : isCapturingScreenshots
              ? t('common.active')
              : `${t('common.paused')}・${t('settings.screenshotCapture.pausedSuggestions')}`}
      </div>

      <button
        type="button"
        className="app-recording-settings-link"
        onClick={() => {
          onOpenSettings();
          navigate('/settings?section=screenshots');
        }}
      >
        <Settings2 size={14} aria-hidden="true" />
        <span>{t('settings.recordingFilter.openSettings')}</span>
      </button>
    </>
  );
}
