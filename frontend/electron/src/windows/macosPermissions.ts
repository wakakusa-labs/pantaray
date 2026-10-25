import { app, shell, systemPreferences } from 'electron';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { CapturePermission } from '../context/zaneiProcess';

const run = promisify(execFile);

/**
 * Electron makes neither request, so a JXA call to the macOS function makes it. osascript runs
 * as this app's child, so macOS attributes the request to this app and lists it.
 */
const ASK_SCRIPTS = {
  // 1 is kIOHIDRequestTypeListenEvent.
  observe_input: 'ObjC.import("IOKit"); $.IOHIDRequestAccess(1)',
  // CoreGraphics is not in the JXA bridge metadata, so the function is bound by hand.
  capture_screen:
    'ObjC.import("CoreGraphics"); ObjC.bindFunction("CGRequestScreenCaptureAccess", ["bool", []]); $.CGRequestScreenCaptureAccess()',
} as const;

const ASK_TIMEOUT_MS = 10_000;

async function askWithScript(script: string): Promise<void> {
  try {
    await run('/usr/bin/osascript', ['-l', 'JavaScript', '-e', script], {
      timeout: ASK_TIMEOUT_MS,
    });
  } catch (error) {
    // Only logged: the user can still open the pane from the waiting row.
    console.error('Permission request failed:', error);
  }
}

const SETTINGS_PANES = {
  read_accessibility_tree: 'Privacy_Accessibility',
  observe_input: 'Privacy_ListenEvent',
  capture_screen: 'Privacy_ScreenCapture',
  automate_browser: 'Privacy_Automation',
  automate_safari: 'Privacy_Automation',
} as const satisfies Record<CapturePermission, string>;

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
export function openCapturePermissionSettings(permission: CapturePermission): Promise<void> {
  return shell.openExternal(
    `x-apple.systempreferences:com.apple.preference.security?${SETTINGS_PANES[permission]}`
  );
}

/**
 * Asks macOS for this permission. macOS shows its own popup, lists the app in the pane around
 * it, and offers to open that pane, so opening the pane here as well would show the user a
 * second, still empty list first.
 */
export async function askForCapturePermission(permission: CapturePermission): Promise<void> {
  switch (permission) {
    case 'read_accessibility_tree':
      // `true` adds the app to the list and shows the system prompt.
      systemPreferences.isTrustedAccessibilityClient(true);
      return;
    case 'observe_input':
      return askWithScript(ASK_SCRIPTS.observe_input);
    case 'capture_screen':
      return askWithScript(ASK_SCRIPTS.capture_screen);
    case 'automate_browser':
    case 'automate_safari':
      // Asked for only once denied, when macOS no longer asks and only the pane can undo it.
      return openCapturePermissionSettings(permission);
  }
}
