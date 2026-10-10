import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';

type PageStatus = 'queued' | 'processing' | 'success' | 'error' | 'canceled';

/** A one-run conversation page in the given state; the run is `runId`. */
export function createActionPage(
  actionId: string,
  status: PageStatus,
  runId = 'run-1'
): ActionConversationPage {
  const finished = status !== 'queued' && status !== 'processing';
  return {
    action: {
      action_id: actionId,
      suggestion_id: null,
      approved_suggestion: null,
      status,
      latest_run_id: runId,
      resumable: status === 'canceled',
    },
    runs: [
      {
        run_id: runId,
        status: finished ? status : 'running',
        started_at: '2026-10-10T00:00:01.000000Z',
        completed_at: finished ? '2026-10-10T00:00:05.000000Z' : null,
        completion_event_id: finished ? `event-${runId}` : null,
        entries: [],
        final_output: status === 'success' ? `answer of ${runId}` : null,
        error:
          status === 'error'
            ? { code: 'ACTION_FAILED', message: 'failed' }
            : status === 'canceled'
              ? { code: 'ACTION_CANCELED', message: 'stopped' }
              : null,
      },
    ],
    unadopted_messages: [],
    next_cursor: null,
  };
}
