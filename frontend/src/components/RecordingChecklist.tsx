import { Check } from 'lucide-react';
import { type ReactNode, useId, useState } from 'react';

import type {
  CapturePermission,
  CapturePermissionCheck,
} from '../../electron/src/context/zaneiProcess';
import type { MessageKey } from '../i18n/types';
import type { Translate } from '../pages/settings/types';
import '../pages/settings/settingsPage.css';
import './recordingChecklist.css';

const WAITING_COPY = {
  read_accessibility_tree: 'onboarding.recording.accessibilityWaiting',
  observe_input: 'onboarding.recording.inputMonitoringWaiting',
  capture_screen: 'onboarding.recording.screenRecordingWaiting',
  automate_browser: 'onboarding.recording.chromeWaiting',
  automate_safari: 'onboarding.recording.safariWaiting',
} as const satisfies Record<CapturePermission, MessageKey>;

/** One checklist row: its mark, its title with at most one note, and its one action. */
export function CheckItem({
  title,
  note,
  problem = false,
  done,
  action,
}: {
  title: string;
  note: string | null;
  /** The note says why the row is stuck, so it is drawn as a warning and announced. */
  problem?: boolean;
  done: boolean;
  action: (titleId: string) => ReactNode;
}) {
  const titleId = useId();
  return (
    <li className={done ? 'recording-check recording-check--done' : 'recording-check'}>
      <span className="recording-check-mark" aria-hidden="true">
        {done ? <Check size={13} strokeWidth={2.4} /> : null}
      </span>
      <span className="recording-check-text">
        <span className="recording-check-title" id={titleId}>
          {title}
        </span>
        {note ? (
          <span
            className={
              problem
                ? 'recording-check-note recording-check-note--problem'
                : 'recording-check-note'
            }
            role={problem ? 'alert' : undefined}
          >
            {note}
          </span>
        ) : null}
      </span>
      <span className="recording-check-action">{action(titleId)}</span>
    </li>
  );
}

/**
 * The macOS permissions a recording start waits on, one row each with a way to grant it,
 * followed by the caller's own rows.
 */
export function RecordingChecklist({
  t,
  checks,
  titles,
  children,
}: {
  t: Translate;
  checks: CapturePermissionCheck[];
  titles: Record<CapturePermission, MessageKey>;
  children?: ReactNode;
}) {
  // Which permissions were asked for here: macOS reports nothing until the user acts there.
  const [requested, setRequested] = useState<ReadonlySet<CapturePermission>>(new Set());
  const allowId = useId();

  const askFor = async (permission: CapturePermission) => {
    setRequested((current) => new Set(current).add(permission));
    try {
      await window.electron?.screenshot?.askForPermission(permission);
    } catch (error) {
      // The row stays waiting with System Settings one press away.
      console.error('Failed to ask for the permission:', error);
    }
  };

  return (
    <ul className="recording-checklist">
      {checks.map(({ permission, granted }) => {
        const waiting = requested.has(permission);
        const buttonId = `${allowId}-${permission}`;
        return (
          <CheckItem
            key={permission}
            title={t(titles[permission])}
            note={!granted && waiting ? t(WAITING_COPY[permission]) : null}
            done={granted}
            action={(titleId) =>
              granted ? (
                <span className="recording-check-state">{t('onboarding.recording.granted')}</span>
              ) : (
                // One button in both states, so focus stays on it after the first press.
                <button
                  type="button"
                  id={buttonId}
                  className={waiting ? 'settings-text-button' : 'settings-action-button'}
                  aria-labelledby={`${buttonId} ${titleId}`}
                  onClick={() => void askFor(permission)}
                >
                  {t(waiting ? 'recordingIntro.openSettings' : 'onboarding.recording.allow')}
                </button>
              )
            }
          />
        );
      })}
      {children}
    </ul>
  );
}
