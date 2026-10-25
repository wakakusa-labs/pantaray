import { TriangleAlert } from 'lucide-react';
import { useRef, useState, type RefObject } from 'react';
import { useNavigate } from 'react-router-dom';

import { useI18n } from '@/context/useI18n';
import { useDismissablePopover } from '@/pages/settings/useDismissablePopover';
import { useCapturePermissionRequired } from '@/pages/settings/useCapturePermissionRequired';
import { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';
import { RecordingControls } from './RecordingControls';

const POPOVER_ID = 'app-recording-popover';

/**
 * The rail's recording light, and the popover that holds the recording controls. While
 * recording cannot run for want of a macOS permission, the light carries a warning and
 * opens the recording settings, where the permissions are granted, instead.
 */
export function RecordingRailControl() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const capture = useScreenshotCaptureStatus();
  const permissionRequired = useCapturePermissionRequired();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  const label = capture.captureStatusUnavailable
    ? t('settings.screenshotCapture.unavailable')
    : t('layout.recordingStatus', {
        status: permissionRequired
          ? t('settings.screenshotCapture.permissionRequired')
          : capture.isCapturingScreenshots === null
            ? t('settings.loadingStatus')
            : t(capture.isCapturingScreenshots ? 'common.on' : 'common.off'),
      });

  return (
    <div className="app-recording">
      <button
        ref={triggerRef}
        type="button"
        className="app-rail-item"
        aria-label={label}
        data-tooltip={label}
        aria-haspopup={permissionRequired ? undefined : 'dialog'}
        aria-expanded={permissionRequired ? undefined : isOpen}
        aria-controls={isOpen && !permissionRequired ? POPOVER_ID : undefined}
        onClick={() => {
          if (!permissionRequired) {
            setIsOpen((current) => !current);
            return;
          }
          setIsOpen(false);
          navigate('/settings?section=screenshots');
        }}
      >
        <span
          className={[
            'app-recording-light',
            capture.isCapturingScreenshots && !permissionRequired
              ? 'app-recording-light--on'
              : null,
          ]
            .filter(Boolean)
            .join(' ')}
          aria-hidden="true"
        />
        {permissionRequired ? (
          <TriangleAlert
            className="app-recording-alert"
            size={13}
            strokeWidth={2.2}
            aria-hidden="true"
          />
        ) : null}
      </button>
      {isOpen && !permissionRequired ? (
        <RecordingPopover
          capture={capture}
          triggerRef={triggerRef}
          onDismiss={() => setIsOpen(false)}
        />
      ) : null}
    </div>
  );
}

function RecordingPopover({
  capture,
  triggerRef,
  onDismiss,
}: {
  capture: ReturnType<typeof useScreenshotCaptureStatus>;
  triggerRef: RefObject<HTMLButtonElement>;
  onDismiss: () => void;
}) {
  const { t } = useI18n();
  const { popoverRef } = useDismissablePopover({ isOpen: true, triggerRef, onDismiss });
  return (
    <div
      ref={popoverRef}
      id={POPOVER_ID}
      className="app-recording-popover"
      role="dialog"
      aria-label={t('settings.screenshotCapture.title')}
      tabIndex={-1}
    >
      <RecordingControls capture={capture} onOpenSettings={onDismiss} />
    </div>
  );
}
