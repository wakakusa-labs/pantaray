import { TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState, type RefObject } from 'react';
import { useNavigate } from 'react-router-dom';

import { useI18n } from '@/context/useI18n';
import { useDismissablePopover } from '@/pages/settings/useDismissablePopover';
import { useCapturePermissionRequired } from '@/pages/settings/useCapturePermissionRequired';
import { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';
import type { MeetingCall } from '../../electron/src/context/meetingTranscript';
import { RecordingControls } from './RecordingControls';

const POPOVER_ID = 'app-recording-popover';

/** The call the recorder offers or transcribes, as main reports it. */
function useMeetingCall(): MeetingCall {
  const [call, setCall] = useState<MeetingCall>({ kind: 'idle' });
  useEffect(() => {
    const meeting = window.electron?.meeting;
    if (!meeting) return;
    let changed = false;
    const unsubscribe = meeting.onCallChanged((next) => {
      changed = true;
      setCall(next);
    });
    meeting.getCall().then(
      // A change that arrived meanwhile is newer than this read.
      (current) => {
        if (!changed) setCall(current);
      },
      (error: unknown) => console.error('Failed to read the call:', error)
    );
    return unsubscribe;
  }, []);
  return call;
}

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
  const call = useMeetingCall();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  const label = capture.captureStatusUnavailable
    ? t('settings.screenshotCapture.unavailable')
    : call.kind === 'recording'
      ? t('settings.callTranscription.transcribing', { call: call.site ?? call.app.name })
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
            call.kind === 'recording' ? 'app-recording-light--transcribing' : null,
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
          call={call}
          capture={capture}
          triggerRef={triggerRef}
          onDismiss={() => setIsOpen(false)}
        />
      ) : null}
    </div>
  );
}

function RecordingPopover({
  call,
  capture,
  triggerRef,
  onDismiss,
}: {
  call: MeetingCall;
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
      <CallControls call={call} />
      <RecordingControls capture={capture} onOpenSettings={onDismiss} />
    </div>
  );
}

/**
 * A call being transcribed, with the way to stop it; or a call the user let pass, which can
 * still be transcribed while it lasts. The answer arrives as the next call state.
 */
function CallControls({ call }: { call: MeetingCall }) {
  const { t } = useI18n();
  const meeting = window.electron?.meeting;
  if (!meeting || (call.kind !== 'recording' && call.kind !== 'not_recording')) return null;
  const send = (command: (callId: string) => Promise<unknown>) =>
    command(call.call_id).catch((error: unknown) =>
      console.error('Call transcription command failed:', error)
    );
  return (
    <div className="app-recording-call">
      {call.kind === 'recording' ? (
        <>
          <div className="app-recording-title">
            {t('settings.callTranscription.transcribing', { call: call.site ?? call.app.name })}
          </div>
          <button
            type="button"
            className="settings-action-button"
            onClick={() => void send(meeting.stop)}
          >
            {t('settings.callTranscription.stop')}
          </button>
        </>
      ) : (
        <button
          type="button"
          className="settings-action-button"
          onClick={() => void send(meeting.record)}
        >
          {t('settings.callTranscription.transcribeThisCall')}
        </button>
      )}
    </div>
  );
}
