import { useEffect, useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { RecordingStartResult } from '../../electron/src/screenshot/screenshotSync';
import { useCaptureGate } from '@/hooks/useCaptureGate';
import { useScreenshotCaptureStatus } from '@/pages/settings/useScreenshotCaptureStatus';

import './recordingIntro.css';

/**
 * What recording covers, in the order a first-time reader asks it: what it is for,
 * what is taken, what happens in a browser, and what is never taken and where it goes.
 *
 * This screen states what is true in a user's words and leaves the mechanics to the
 * help center. Page text is kept out of the browsers that report no private window
 * (`PRIVATE_WINDOW_UNDETECTABLE_BROWSER_BUNDLE_IDS` in the recorder, which
 * `zaneiConfig` leaves at its default), while every browser is still recorded like an
 * ordinary app, titles included — so the browser line never claims that a browser is
 * not recorded.
 */
const BODY_KEYS = [
  'recordingIntro.purpose',
  'recordingIntro.captured',
  'recordingIntro.browsers',
  'recordingIntro.excluded',
] as const;

/**
 * The screen that asks macOS for the permissions the recorder needs.
 *
 * Until they are granted the app can read nothing, so no conversation window opens
 * at all: this screen is what a request that was stopped by that gate turns into,
 * and it opens only then — first-run setup is where recording is turned on otherwise.
 * Turning recording on is what asks macOS for the permissions, so this is that toggle,
 * with the words that explain it. "Later" drops the waiting conversation in main.
 */
export function RecordingIntroDialog() {
  const { t } = useI18n();
  const { gate, dismissIntro } = useCaptureGate();
  const { isScreenshotCaptureAvailable, startRecording } = useScreenshotCaptureStatus();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [startOutcome, setStartOutcome] = useState<RecordingStartResult | null>(null);
  // Granted permissions close the screen — including one left open while they were
  // granted for a start made from the tray or the settings page, which the re-read
  // on focus reports. The first permission still missing is the pane its settings
  // button opens.
  const missingPermission =
    gate === null || !gate.introRequested
      ? null
      : !gate.permissions.accessibility
        ? 'read_accessibility_tree'
        : !gate.permissions.screenRecording
          ? 'capture_screen'
          : null;
  const isOpen = missingPermission !== null;

  // Answering the screen closes it and tells main to drop the conversation the gate
  // deferred.
  const answer = async (): Promise<void> => {
    await dismissIntro();
  };

  // The screen is unmounted rather than left closed, so a closed screen leaves
  // nothing behind for assistive technology to reach — and nothing of the attempt
  // that closed it either: its notice belongs to that attempt, and the permissions
  // can be taken away again, which puts this screen back in front.
  useEffect(() => {
    if (!isOpen) {
      setStartOutcome(null);
      return;
    }
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, [isOpen]);

  if (missingPermission === null) return null;

  /**
   * Turning recording on is what makes macOS ask for its permissions, so this button
   * is the whole point of the screen: it is answered only when recording actually
   * started. Any other outcome leaves it open with the reason and the button usable,
   * instead of locking the user out of the conversation they asked for. A start still
   * waiting on macOS says what to grant and offers the pane to grant it in.
   */
  const start = async () => {
    setIsStarting(true);
    setStartOutcome(null);
    let result: RecordingStartResult = 'failed';
    try {
      result = await startRecording();
    } finally {
      setIsStarting(false);
    }
    if (result === 'started') {
      await answer();
      return;
    }
    setStartOutcome(result);
  };

  return (
    <dialog
      ref={dialogRef}
      className="recording-intro-dialog"
      aria-labelledby="recording-intro-title"
      // Escape must not leave the screen unanswered, so it counts as "later" —
      // and never while a start it cannot cancel is still running.
      onCancel={(event) => {
        event.preventDefault();
        if (isStarting) return;
        void answer();
      }}
    >
      <h2 id="recording-intro-title">{t('recordingIntro.title')}</h2>
      {BODY_KEYS.map((key) => (
        <p key={key} className="recording-intro-body">
          {t(key)}
        </p>
      ))}
      <div className="recording-intro-actions">
        <button
          type="button"
          className="recording-intro-primary"
          disabled={isStarting || !isScreenshotCaptureAvailable}
          onClick={() => void start()}
        >
          {t('recordingIntro.start')}
        </button>
        <button
          type="button"
          className="recording-intro-later"
          disabled={isStarting}
          onClick={() => void answer()}
        >
          {t('recordingIntro.later')}
        </button>
      </div>
      <p className="recording-intro-note">{t('recordingIntro.settingsNote')}</p>
      {!isScreenshotCaptureAvailable ? (
        <p className="recording-intro-unavailable">{t('settings.screenshotCapture.unavailable')}</p>
      ) : startOutcome !== null ? (
        <p className="recording-intro-unavailable" role="alert">
          {t(
            startOutcome === 'permission_pending'
              ? 'recordingIntro.permissionPending'
              : 'recordingIntro.startFailed'
          )}
        </p>
      ) : null}
      {startOutcome === 'permission_pending' ? (
        <button
          type="button"
          className="recording-intro-settings"
          onClick={() => void window.electron?.screenshot?.askForPermission?.(missingPermission)}
        >
          {t('recordingIntro.openSettings')}
        </button>
      ) : null}
    </dialog>
  );
}
