import { describe, expect, it } from 'vitest';

import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';
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
  // The user approves a hook that sends with no one watching: a wrong or missing line
  // here means saying yes to a different hook than the one that is saved.
  describe('hook', () => {
    const ja = (key: MessageKey, vars?: Record<string, string | number>) =>
      translate('ja', key, vars);
    const hook = (fields: Record<string, unknown>) =>
      buildApprovalDisplay(
        blocker('create_hook', 'automation_control', {
          summary_kind: 'hook',
          replaces: false,
          resumes: false,
          trigger: { kind: 'every', every: 'day', offset: '510m' },
          until: null,
          send_to: 'this_session',
          place: null,
          title: null,
          message: 'Look for news.',
          ...fields,
        }),
        ja
      );
    const details = (display: ReturnType<typeof hook>) =>
      display.details.map((detail) => [ja(detail.labelKey), detail.value]);
    const on = (match: unknown[]) => hook({ trigger: { kind: 'on', on: 'activity', match } });

    it('says when a schedule fires, from calendar units and offsets alike', () => {
      const when = (trigger: Record<string, unknown>) => hook({ trigger }).primaryValue;
      expect(when({ kind: 'every', every: 'day', offset: '510m' })).toBe('毎日 8:30');
      expect(when({ kind: 'every', every: 'week', offset: '57h' })).toBe('毎週水曜 9:00');
      expect(when({ kind: 'every', every: 'month', offset: '2d' })).toBe('毎月 3 日 0:00');
      expect(when({ kind: 'every', every: 'quarter', offset: '27d' })).toBe(
        '1・4・7・10 月の 28 日 0:00'
      );
      expect(when({ kind: 'every', every: 'quarter', offset: '31d' })).toBe(
        '1・4・7・10 月の 1 日から 31 日後 0:00'
      );
      expect(when({ kind: 'every', every: '2h', offset: null })).toBe('2 時間ごと');
      const at = new Date(2026, 10, 22, 9, 0).toISOString();
      expect(when({ kind: 'at', at })).toBe('2026-11-22 09:00 に1回');
    });

    it('shows the end, where the message goes, and the exact message', () => {
      const daily = hook({});
      expect(daily.operationKey).toBe('overlay.approvalRequired.operation.hook');
      expect(details(daily)).toEqual([['送り先', 'この会話を続けます']]);
      expect(daily.argumentFields).toEqual([
        { label: '送る内容', format: 'text', value: 'Look for news.' },
      ]);

      const until = new Date(2026, 11, 1, 9, 0).toISOString();
      const fresh = hook({
        until,
        send_to: 'new_session',
        place: { organization: 'Wakakusa', project: 'Pantaray' },
        title: 'Morning news',
      });
      expect(details(fresh)).toEqual([
        ['終わり', '2026-12-01 09:00'],
        ['送り先', '毎回新しい会話を始めます'],
        ['毎回作る TODO', 'Morning news'],
        ['組織', 'Wakakusa'],
        ['プロジェクト', 'Pantaray'],
      ]);
      // No place puts the conversation in neither, which the user should see too.
      expect(
        details(hook({ send_to: 'new_session', place: null, title: 'Morning news' })).slice(2)
      ).toEqual([
        ['組織', 'なし'],
        ['プロジェクト', 'なし'],
      ]);
    });

    it('keeps the change and turn-back-on questions apart from a new hook', () => {
      expect(hook({ replaces: true }).operationKey).toBe(
        'overlay.approvalRequired.operation.hookUpdate'
      );
      expect(hook({ replaces: true, resumes: true }).operationKey).toBe(
        'overlay.approvalRequired.operation.hookResume'
      );
    });

    // The sentence is the only place the user reads the condition, so any-of within a
    // field, all-of across fields, and equals against contains must each read as such.
    it('words an activity condition from the stored match', () => {
      const meeting = on([
        { field: 'bundle_id', op: 'equals', values: ['us.zoom.xos'] },
        { field: 'window_title', op: 'contains', values: ['Zoom ミーティング', 'Zoom Meeting'] },
      ]);
      expect(meeting.primaryValue).toBe(
        'アプリの識別子が『us.zoom.xos』で、ウィンドウの題名に『Zoom ミーティング』か『Zoom Meeting』を含むウィンドウになったとき'
      );
      expect(
        on([
          { field: 'window_title', op: 'contains', values: ['Meet'] },
          { field: 'app_name', op: 'equals', values: ['Google Chrome', 'Arc'] },
        ]).primaryValue
      ).toBe(
        'ウィンドウの題名に『Meet』を含み、アプリの名前が『Google Chrome』か『Arc』のウィンドウになったとき'
      );
      const english = buildApprovalDisplay(
        blocker('create_hook', 'automation_control', {
          summary_kind: 'hook',
          trigger: {
            kind: 'on',
            on: 'activity',
            match: [
              { field: 'bundle_id', op: 'equals', values: ['us.zoom.xos'] },
              {
                field: 'window_title',
                op: 'contains',
                values: ['Zoom Meeting', 'Zoom ミーティング'],
              },
            ],
          },
          send_to: 'this_session',
          message: 'Take notes.',
        }),
        (key, vars) => translate('en', key, vars)
      );
      expect(english.primaryValue).toBe(
        'When a window starts to match: app ID is “us.zoom.xos”, and title contains “Zoom Meeting” or “Zoom ミーティング”'
      );
    });

    // Only an activity hook sends the matched window along; a schedule must not claim it.
    it('says the matched window goes with the message only for an activity hook', () => {
      const alsoSent = ['いっしょに送るもの', '見つけたウィンドウのアプリの名前と識別子、題名'];
      expect(
        details(on([{ field: 'app_name', op: 'equals', values: ['zoom.us'] }]))
      ).toContainEqual(alsoSent);
      expect(details(hook({}))).not.toContainEqual(alsoSent);
    });
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
