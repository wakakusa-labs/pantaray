import { type ReactNode, useId, useState } from 'react';

import type {
  CapturePermission,
  CapturePermissionCheck,
} from '../../electron/src/context/zaneiProcess';
import type { MessageKey } from '../i18n/types';
import type { Translate } from '../pages/settings/types';
import '../pages/settings/settingsPage.css';
import { CheckItem } from './Checklist';

const WAITING_COPY = {
  read_accessibility_tree: 'onboarding.recording.accessibilityWaiting',
  observe_input: 'onboarding.recording.inputMonitoringWaiting',
  capture_screen: 'onboarding.recording.screenRecordingWaiting',
  automate_browser: 'onboarding.recording.chromeWaiting',
  automate_safari: 'onboarding.recording.safariWaiting',
} as const satisfies Record<CapturePermission, MessageKey>;

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

  // macOS's own popup guides the user to System Settings after an ask.
  const askFor = async (permission: CapturePermission) => {
    setRequested((current) => new Set(current).add(permission));
    try {
      await window.electron?.screenshot?.askForPermission(permission);
    } catch (error) {
      // The row stays waiting with System Settings one press away.
      console.error('Failed to ask for the permission:', error);
    }
  };

  // For when macOS shows no popup, as after an earlier denial.
  const openSettings = async (permission: CapturePermission) => {
    try {
      await window.electron?.screenshot?.openPermissionSettings(permission);
    } catch (error) {
      console.error('Failed to open System Settings:', error);
    }
  };

  // macOS lets an app trusted for Accessibility observe input, so Input Monitoring usually
  // completes with it; asking first would send the user to a pane they do not need.
  const accessibilityMissing = checks.some(
    (check) => check.permission === 'read_accessibility_tree' && !check.granted
  );

  return (
    <ul className="checklist">
      {checks.map(({ permission, granted }) => {
        const waiting = requested.has(permission);
        const blocked = permission === 'observe_input' && accessibilityMissing;
        const buttonId = `${allowId}-${permission}`;
        return (
          <CheckItem
            key={permission}
            title={t(titles[permission])}
            note={!granted && waiting ? t(WAITING_COPY[permission]) : null}
            status={granted ? 'done' : 'pending'}
            action={(titleId) =>
              granted ? (
                <span className="check-item-state">{t('onboarding.recording.granted')}</span>
              ) : (
                // One button in both states, so focus stays on it after the first press.
                <button
                  type="button"
                  id={buttonId}
                  className={waiting ? 'settings-text-button' : 'settings-action-button'}
                  aria-labelledby={`${buttonId} ${titleId}`}
                  disabled={blocked}
                  onClick={() => void (waiting ? openSettings(permission) : askFor(permission))}
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
