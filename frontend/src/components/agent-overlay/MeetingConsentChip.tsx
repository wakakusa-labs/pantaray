import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';

import overlayBackground from '@/assets/images/agentoverlay_back.png';
import { useI18n } from '@/context/useI18n';
import type { MeetingCall } from '../../../electron/src/context/meetingTranscript';
import { ApprovalDecisionButton } from './ApprovalDecisionButton';
import { PopupContainer } from './OverlayBackground';
import './meetingConsentChip.css';

type OfferedCall = Extract<MeetingCall, { kind: 'awaiting_consent' }>;

/**
 * Asks whether to transcribe the call the recorder detected. The panel opens without focus and
 * has no default button, so a key meant for another app cannot start a recording. Main closes
 * it once the call is answered, the consent expires or the call ends.
 */
export function MeetingConsentChip() {
  const { t } = useI18n();
  const meeting = window.electron?.meeting;
  const [call, setCall] = useState<OfferedCall | null>(null);
  const [answering, setAnswering] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const questionId = useId();
  const noteId = useId();

  useEffect(() => {
    meeting?.getCall().then(
      (current) => setCall(current.kind === 'awaiting_consent' ? current : null),
      (error: unknown) => console.error('Failed to read the call:', error)
    );
  }, [meeting]);

  // The window is sized to the chip's own height, which the window's first size does not cap
  // (see the CSS); main keeps the window on screen. A name that wraps grows it.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const report = () =>
      window.electron?.agentOverlay?.resize(Math.ceil(panel.getBoundingClientRect().height), {
        settled: true,
      });
    report();
    const observer = new ResizeObserver(report);
    observer.observe(panel);
    return () => observer.disconnect();
  }, [call]);

  if (!call || !meeting) return null;

  const answer = (send: (callId: string) => Promise<unknown>) => {
    setAnswering(true);
    send(call.call_id).catch((error: unknown) => {
      console.error('Failed to answer for the call:', error);
      setAnswering(false);
    });
  };

  return (
    <PopupContainer
      ref={panelRef}
      as="section"
      className="meeting-consent"
      $bg={overlayBackground}
      $isVisible
      aria-labelledby={questionId}
      aria-describedby={noteId}
    >
      <p id={questionId} className="meeting-consent__question">
        {t('overlay.meetingConsent.question', { call: call.site ?? call.app.name })}
      </p>
      <p id={noteId} className="meeting-consent__note">
        {t('overlay.meetingConsent.tellOthers')}
      </p>
      <div className="meeting-consent__actions">
        <ApprovalDecisionButton
          type="button"
          $variant="secondary"
          disabled={answering}
          onClick={() => answer(meeting.decline)}
        >
          {t('overlay.meetingConsent.decline')}
        </ApprovalDecisionButton>
        <ApprovalDecisionButton
          type="button"
          $variant="secondary"
          disabled={answering}
          onClick={() => answer(meeting.record)}
        >
          {t('overlay.meetingConsent.record')}
        </ApprovalDecisionButton>
      </div>
    </PopupContainer>
  );
}
