import { app, shell, systemPreferences } from 'electron';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { CapturePermission } from '../context/zaneiProcess';
import type { CapturePermissionPane } from '../ipc/schemas/onboarding';

const run = promisify(execFile);

type SecurityPane =
  | 'Privacy_Accessibility'
  | 'Privacy_ListenEvent'
  | 'Privacy_ScreenCapture'
  | 'Privacy_Automation';

/**
 * Lets the E2E harness stand in for macOS: a CI runner grants no app Accessibility,
 * and TCC cannot be seeded. Honoured only in an unpackaged build, so a shipped app
 * reads the real permission whatever its environment says.
 */
const ASSUME_PERMISSIONS_ENV = 'PANTARAY_E2E_ASSUME_CAPTURE_PERMISSIONS';

const assumesPermissions = () => !app.isPackaged && process.env[ASSUME_PERMISSIONS_ENV] === '1';

/**
 * The first macOS major version on which the recorder can capture a window
 * (ScreenCaptureKit). Below it the recorder reports `capture_screen` as unavailable:
 * there is nothing to grant, so the permission cannot hold recording back.
 */
const SCREEN_RECORDING_MIN_MACOS_MAJOR = 14;

function openSecurityPane(pane: SecurityPane): Promise<void> {
  return shell.openExternal(`x-apple.systempreferences:com.apple.preference.security?${pane}`);
}

/**
 * Whether this app holds Screen Recording, which the recorder uses to read the text of
 * windows that Accessibility leaves blank. Recording requires it wherever macOS can
 * grant it.
 *
 * macOS applies a grant to a running process only after it reopens, so a grant made
 * while the app runs reads as missing until the app is reopened.
 */
export function holdsScreenRecording(): boolean {
  if (assumesPermissions()) return true;
  if (Number(process.getSystemVersion().split('.')[0]) < SCREEN_RECORDING_MIN_MACOS_MAJOR) {
    return true;
  }
  return systemPreferences.getMediaAccessStatus('screen') === 'granted';
}

export type CapturePermissions = { accessibility: boolean; screenRecording: boolean };

/**
 * The two permissions the recorder needs that macOS reports to this process:
 * Accessibility to read a single window, and Screen Recording for windows that
 * Accessibility leaves blank. Everything the assistant knows comes from the recorder,
 * so both together are the gate the whole product stands behind. Read live because
 * the user grants them in System Settings, outside the app. Input Monitoring and
 * browser Automation are asked for by the recorder's own start check and surface
 * through the capture status instead.
 */
export function readCapturePermissions(): CapturePermissions {
  if (assumesPermissions()) return { accessibility: true, screenRecording: true };
  if (process.platform !== 'darwin') return { accessibility: false, screenRecording: false };
  return {
    accessibility: systemPreferences.isTrustedAccessibilityClient(false),
    screenRecording: holdsScreenRecording(),
  };
}

/** Opens the System Settings pane where the user grants this permission. */
export function openCapturePermissionSettings(permission: CapturePermissionPane): Promise<void> {
  return openSecurityPane(
    permission === 'screen_recording' ? 'Privacy_ScreenCapture' : 'Privacy_Accessibility'
  );
}

/** Called only by an explicit recording start. */
export async function requestCapturePermissions(missing: CapturePermission[]): Promise<void> {
  for (const [capability, bundleId] of [
    ['automate_browser', 'com.google.Chrome'],
    ['automate_safari', 'com.apple.Safari'],
  ] as const) {
    if (!missing.includes(capability)) continue;
    try {
      // Sending a harmless AppleEvent makes this browser appear in Pantaray's Automation list.
      await run('/usr/bin/osascript', ['-e', `tell application id "${bundleId}" to get name`], {
        timeout: 10_000,
      });
    } catch (error) {
      // User denial is the expected permission-required result; other failures remain visible.
      if (
        !(error instanceof Error && 'stderr' in error && String(error.stderr).includes('-1743'))
      ) {
        throw error;
      }
    }
  }
  await openSecurityPane(
    missing.includes('read_accessibility_tree')
      ? 'Privacy_Accessibility'
      : missing.includes('observe_input')
        ? 'Privacy_ListenEvent'
        : missing.includes('capture_screen')
          ? 'Privacy_ScreenCapture'
          : 'Privacy_Automation'
  );
}
