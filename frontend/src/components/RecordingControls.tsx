import { Settings2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { useI18n } from '@/context/useI18n';
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

  const isToggleDisabled =
    isProcessing || (!isCapturingScreenshots && !isScreenshotCaptureAvailable);

  return (
    <>
      <div className="app-recording-row">
        <div className="app-recording-title">{t('settings.screenshotCapture.title')}</div>
        {/* role="switch" cannot express an unknown state, so the switch appears only
            once the status IPC has answered; the placeholder holds the row height. */}
        {isCapturingScreenshots === null ? (
          <span className="settings-toggle settings-toggle--compact" aria-hidden="true" />
        ) : (
          <button
            type="button"
            role="switch"
            aria-checked={isCapturingScreenshots}
            aria-label={t('settings.screenshotCapture.screenshotsLabel')}
            className={[
              'settings-toggle',
              'settings-toggle--compact',
              isCapturingScreenshots ? 'settings-toggle--on' : null,
            ]
              .filter(Boolean)
              .join(' ')}
            disabled={isToggleDisabled}
            onClick={() => void setScreenshotsEnabled(!isCapturingScreenshots)}
          >
            <span className="settings-toggle-thumb" />
            <span className="settings-toggle-label">
              {isCapturingScreenshots ? t('common.on') : t('common.off')}
            </span>
          </button>
        )}
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
