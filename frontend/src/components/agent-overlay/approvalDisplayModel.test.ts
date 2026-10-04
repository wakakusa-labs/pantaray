import { describe, expect, it } from 'vitest';

import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';
import { buildApprovalDisplay } from './approvalDisplayModel';

const t = (key: string) => key;

// The panel is chosen from the tool and its summary; the intent class only routes the
// consent scope in the runtime, so it is carried along unread.
function blocker(
  toolId: string,
  intentClass: string,
  commandSummary: ActionApprovalBlocker['commandSummary']
): ActionApprovalBlocker {
  return {
    actionId: 'action-1',
    processId: 'process-1',
    approvalSessionId: 'approval-1',
    toolRequestId: 'request-1',
    toolId,
    intentClass,
    commandSummary,
  };
}

describe('buildApprovalDisplay', () => {
  // What is being consented to must be readable: a screen capture that falls through
  // to the generic line says only "run this operation" and hides the disclosure.
  it('names the window capture and its app, from the tool id and from a recovered summary alike', () => {
    const summary = { summary_kind: 'screen_capture', app_name: 'Google Chrome' };
    for (const panel of [
      blocker('capture_screen', 'screen_capture', summary),
      blocker('unknown_tool', 'screen_capture', summary),
    ]) {
      const display = buildApprovalDisplay(panel, t);
      expect(display.operationKey).toBe('overlay.approvalRequired.operation.captureScreen');
      expect(display.primaryLabelKey).toBe('overlay.approvalRequired.app');
      expect(display.primaryValue).toBe('Google Chrome');
      expect(display.details).toEqual([]);
    }
    expect(
      buildApprovalDisplay(blocker('capture_screen', 'screen_capture', {}), t).primaryValue
    ).toBe('overlay.approvalRequired.unavailable');
  });

  // A command that runs with the user's sign-ins must say so before it is approved.
  it('says when a command uses the login environment', () => {
    const summary = { summary_kind: 'bash', command: 'gh auth status', cwd: '/repo' };
    expect(
      buildApprovalDisplay(
        blocker('bash', 'process_exec_local', { ...summary, use_login_environment: true }),
        t
      ).operationKey
    ).toBe('overlay.approvalRequired.operation.bashLoginEnvironment');
    expect(
      buildApprovalDisplay(
        blocker('bash', 'process_exec_local', { ...summary, use_login_environment: false }),
        t
      ).operationKey
    ).toBe('overlay.approvalRequired.operation.bash');
  });

  it('still falls back to the generic line for a tool it does not know', () => {
    const display = buildApprovalDisplay(
      blocker('web_fetch', 'read_only', { url: 'https://example.com' }),
      t
    );

    expect(display.operationKey).toBe('overlay.approvalRequired.operation.generic');
    expect(display.primaryValue).toBe('url: https://example.com');
  });
});
