import { describe, expect, it } from 'vitest';

import { projectActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import type { ActionLiveSnapshot } from '../../../electron/src/actions/actionLiveCore';
import { createActionPage } from './actionTaskFixtures';
import { deriveActionTaskStatus } from './actionTaskStatus';

type Lifecycle = ActionLiveSnapshot['lifecycle'];

function derive(
  lifecycle: Lifecycle,
  page: ReturnType<typeof createActionPage> | null,
  approvalPending = false
) {
  const view = page ? projectActionConversationView([page]) : null;
  return deriveActionTaskStatus({ lifecycle, page, approvalPending, view });
}

const running: Lifecycle = { processId: 'proc-live', status: 'processing' };

describe('deriveActionTaskStatus', () => {
  it.each([
    ['nothing read yet', null, null, false, 'idle', null],
    ['a queued page', null, createActionPage('a', 'queued'), false, 'running', 'run-1'],
    ['a processing page', null, createActionPage('a', 'processing'), false, 'running', 'run-1'],
    ['a successful page', null, createActionPage('a', 'success'), false, 'idle', null],
    ['a canceled page', null, createActionPage('a', 'canceled'), false, 'idle', null],
    ['a failed page', null, createActionPage('a', 'error'), false, 'failed', null],
    // The lifecycle wins over the page it arrived after, both ways.
    [
      'a run started after a finished page',
      running,
      createActionPage('a', 'success'),
      false,
      'running',
      'proc-live',
    ],
    [
      'a run finished before its page',
      { processId: 'run-1', status: 'success' },
      createActionPage('a', 'processing'),
      false,
      'idle',
      null,
    ],
    [
      'a timed-out run',
      { processId: 'run-1', status: 'timeout' },
      createActionPage('a', 'processing'),
      false,
      'failed',
      null,
    ],
    [
      'a running page awaiting approval',
      null,
      createActionPage('a', 'processing'),
      true,
      'approval_pending',
      'run-1',
    ],
    ['a running lifecycle awaiting approval', running, null, true, 'approval_pending', 'proc-live'],
  ] as const)('%s', (_name, lifecycle, page, approvalPending, kind, stopTarget) => {
    const status = derive(lifecycle, page, approvalPending);
    expect(status.kind).toBe(kind);
    expect(status.stopTarget).toBe(stopTarget);
  });

  it('offers resume only while the canonical page says the stop is the latest intent', () => {
    expect(derive(null, createActionPage('a', 'canceled')).resumable).toBe(true);
    // A run started after the stop: the page is stale until its read lands.
    expect(derive(running, createActionPage('a', 'canceled')).resumable).toBe(false);
    expect(derive(null, createActionPage('a', 'success')).resumable).toBe(false);
  });

  it('thinks until the current run shows an outcome', () => {
    expect(derive(null, createActionPage('a', 'processing')).thinking).toBe(true);
    expect(derive(null, createActionPage('a', 'success')).thinking).toBe(false);
  });

  it('marks a failure as unexplained until its outcome is in the conversation', () => {
    const failedRun: Lifecycle = { processId: 'run-1', status: 'error' };
    expect(derive(failedRun, createActionPage('a', 'processing')).failedWithoutOutcome).toBe(true);
    expect(derive(null, createActionPage('a', 'error')).failedWithoutOutcome).toBe(false);
  });

  it('reads the completion of the current run only once its outcome shows', () => {
    expect(derive(null, createActionPage('a', 'success')).completionEventId).toBe('event-run-1');
    expect(derive(null, createActionPage('a', 'processing')).completionEventId).toBeNull();
    // A newer run started: the previous run's completion is no longer the current one.
    expect(derive(running, createActionPage('a', 'success')).completionEventId).toBeNull();
  });
});
