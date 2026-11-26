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
    reviewReason: null,
  };
}

describe('buildApprovalDisplay', () => {
  // A schedule that falls through to the generic line shows raw keys, so the user
  // could not tell when it runs or what it sends before saying yes.
  it('names when a schedule runs, when it ends, and the message it sends', () => {
    const vars = (key: string, values?: Record<string, string | number>) =>
      key.endsWith('weekdays')
        ? 'Mon,Tue,Wed,Thu,Fri,Sat,Sun'
        : `${key}${values ? JSON.stringify(values) : ''}`;
    const schedule = (fields: Record<string, unknown>) =>
      buildApprovalDisplay(
        blocker('schedule_continuation', 'automation_control', {
          summary_kind: 'conversation_task',
          task_id: 'task.conversation-1',
          replaces: false,
          resumes: false,
          at: null,
          every: null,
          offset: null,
          until: null,
          prompt: 'Look for news.',
          ...fields,
        }),
        vars
      );
    const daily = schedule({ every: 'day', offset: '510m' });
    expect(daily.operationKey).toBe('overlay.approvalRequired.operation.conversationTask');
    expect(daily.primaryValue).toBe(
      'overlay.approvalRequired.conversationTask.everyDay{"time":"8:30"}'
    );
    expect(daily.argumentFields).toEqual([
      {
        label: 'overlay.approvalRequired.conversationTask.prompt',
        format: 'text',
        value: 'Look for news.',
      },
    ]);
    expect(daily.details).toEqual([]);
    expect(schedule({ every: 'week', offset: '57h' }).primaryValue).toBe(
      'overlay.approvalRequired.conversationTask.everyWeek{"weekday":"Wed","time":"9:00"}'
    );
    expect(schedule({ every: 'month', offset: '2d' }).primaryValue).toContain('"day":"3"');
    expect(schedule({ every: 'quarter', offset: '27d' }).primaryValue).toContain('"day":"28"');
    expect(schedule({ every: 'quarter', offset: '31d' }).primaryValue).toBe(
      'overlay.approvalRequired.conversationTask.everyQuarterAfter{"days":"31","time":"0:00"}'
    );
    const changed = schedule({
      replaces: true,
      every: '2h',
      until: '2026-12-01T00:00:00Z',
    });
    expect(changed.operationKey).toBe('overlay.approvalRequired.operation.conversationTaskUpdate');
    expect(changed.primaryValue).toContain('everyDuration');
    expect(schedule({ replaces: true, resumes: true, every: 'day' }).operationKey).toBe(
      'overlay.approvalRequired.operation.conversationTaskResume'
    );
    expect(changed.details.map((detail) => detail.labelKey)).toEqual([
      'overlay.approvalRequired.conversationTask.until',
    ]);
    expect(schedule({ at: '2026-11-22T09:00:00+09:00' }).primaryValue).toContain(
      'conversationTask.once'
    );
  });

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

  // A browser write that falls through to the generic line shows raw summary keys and
  // hides where the data goes and which page sends it.
  it('names where a browser write sends data, the page it comes from and the reason', () => {
    const display = buildApprovalDisplay(
      blocker('browser', 'read_only', {
        summary_kind: 'browser_write',
        host: 'api.example.com',
        page_host: 'localhost',
        reason: 'Send the signup form you asked me to test.',
      }),
      t
    );
    expect(display.operationKey).toBe('overlay.approvalRequired.operation.browserWrite');
    expect(display.reason).toBe('Send the signup form you asked me to test.');
    expect(display.primaryLabelKey).toBe('overlay.approvalRequired.sendsTo');
    expect(display.primaryValue).toBe('api.example.com');
    expect(display.details).toEqual([
      { labelKey: 'overlay.approvalRequired.openPage', value: 'localhost' },
    ]);
    expect(
      buildApprovalDisplay(
        blocker('browser', 'read_only', {
          summary_kind: 'browser_write',
          host: 'api.example.com',
          page_host: null,
          reason: 'x',
        }),
        t
      ).details
    ).toEqual([]);
  });

  // Catches: the user's browser approved under the model's reason alone, so the user
  // never reads that the site is opened or written to signed in as them.
  it("leads a user's-browser approval with Pantaray's line naming the browser", () => {
    const read = buildApprovalDisplay(
      blocker('browser', 'read_only', {
        summary_kind: 'user_browser_read',
        browser: 'Google Chrome',
        host: 'mail.example.com',
        reason: 'Read the invoice email you mentioned.',
      }),
      t
    );
    expect(read.operationKey).toBe('overlay.approvalRequired.operation.userBrowserRead');
    expect(read.operationVars).toEqual({ browser: 'Google Chrome' });
    expect(read.reason).toBeNull();
    expect(read.primaryLabelKey).toBe('overlay.approvalRequired.site');
    expect(read.primaryValue).toBe('mail.example.com');
    expect(read.details).toEqual([
      { labelKey: 'overlay.approvalRequired.why', value: 'Read the invoice email you mentioned.' },
    ]);

    const write = buildApprovalDisplay(
      blocker('browser', 'read_only', {
        summary_kind: 'browser_write',
        browser: 'Google Chrome',
        host: 'api.example.com',
        page_host: 'shop.example.com',
        reason: 'Place the order you asked for.',
      }),
      t
    );
    expect(write.operationKey).toBe('overlay.approvalRequired.operation.userBrowserWrite');
    expect(write.operationVars).toEqual({ browser: 'Google Chrome' });
    expect(write.reason).toBeNull();
    expect(write.primaryLabelKey).toBe('overlay.approvalRequired.sendsTo');
    expect(write.primaryValue).toBe('api.example.com');
    expect(write.details).toEqual([
      { labelKey: 'overlay.approvalRequired.openPage', value: 'shop.example.com' },
      { labelKey: 'overlay.approvalRequired.why', value: 'Place the order you asked for.' },
    ]);
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

  // JSON.parse rounds integers past 2^53 while the backend runs the exact value, so the
  // display must show the source text rather than a rounded number.
  it.each([
    ['top-level', '{\n  "message_id": 9007199254740993,\n  "subject": "Hi"\n}'],
    ['nested', '{\n  "items": [\n    {\n      "quantity": 12345678901234567890\n    }\n  ]\n}'],
  ])('shows integration arguments verbatim when a %s integer is unsafe', (_case, raw) => {
    const display = buildApprovalDisplay(
      blocker('integration_run_tool', 'integration_write', {
        summary_kind: 'integration_tool',
        account: 'work@example.com',
        toolkit: 'Gmail',
        tool: 'Create email draft',
        arguments: raw,
      }),
      t
    );

    expect(display.argumentFields).toEqual([
      { label: 'overlay.approvalRequired.arguments', format: 'json', value: raw },
    ]);
  });
});
