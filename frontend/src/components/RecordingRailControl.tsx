import { useRef, useState, type RefObject } from 'react';

import { useI18n } from '@/context/useI18n';
import { useDismissablePopover } from '@/pages/settings/useDismissablePopover';
import { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';
import { RecordingControls } from './RecordingControls';

const POPOVER_ID = 'app-recording-popover';

/** The rail's recording light, and the popover that holds the recording controls. */
export function RecordingRailControl() {
  const { t } = useI18n();
  const capture = useScreenshotCaptureStatus();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  const label = capture.captureStatusUnavailable
    ? t('settings.screenshotCapture.unavailable')
    : t('layout.recordingStatus', {
        status:
          capture.isCapturingScreenshots === null
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
        title={label}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-controls={isOpen ? POPOVER_ID : undefined}
        onClick={() => setIsOpen((current) => !current)}
      >
        <span
          className={[
            'app-recording-light',
            capture.isCapturingScreenshots ? 'app-recording-light--on' : null,
          ]
            .filter(Boolean)
            .join(' ')}
          aria-hidden="true"
        />
      </button>
      {isOpen ? (
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
