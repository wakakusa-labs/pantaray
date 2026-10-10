import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import type { ActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import type { ActionLiveSnapshot } from '../../../electron/src/actions/actionLiveCore';

export type ActionTaskStatusKind = 'running' | 'approval_pending' | 'idle' | 'failed';

export type ActionTaskStatus = Readonly<{
  kind: ActionTaskStatusKind;
  /**
   * The run a Stop ends, and the `expected_process_id` a follow-up carries: null when no run is
   * going. Present during an approval too, since a follow-up then still joins that run.
   */
  stopTarget: string | null;
  /** The canonical conversation says the user's Stop is still the latest intent. */
  resumable: boolean;
  /** Running, and the current run has shown neither a tool nor an outcome yet. */
  thinking: boolean;
  /** Failed, and the current run's outcome is not in the conversation yet. */
  failedWithoutOutcome: boolean;
  /** The current run's completion, once its outcome is in the conversation. */
  completionEventId: string | null;
}>;

/**
 * One Action's status for the main window. The lifecycle notification is authoritative until the
 * canonical page read replaces it (the live core clears it on each read); the page decides after.
 */
export function deriveActionTaskStatus({
  lifecycle,
  page,
  approvalPending,
  view,
}: {
  lifecycle: ActionLiveSnapshot['lifecycle'];
  page: ActionConversationPage | null;
  approvalPending: boolean;
  view: ActionConversationView | null;
}): ActionTaskStatus {
  const pageStatus = page?.action.status;
  const runStatus =
    lifecycle?.status ?? (pageStatus === 'queued' ? 'processing' : (pageStatus ?? null));
  const stopTarget = lifecycle
    ? lifecycle.status === 'processing'
      ? lifecycle.processId
      : null
    : page && (pageStatus === 'queued' || pageStatus === 'processing')
      ? page.action.latest_run_id
      : null;
  const kind: ActionTaskStatusKind = approvalPending
    ? 'approval_pending'
    : runStatus === 'processing'
      ? 'running'
      : runStatus === 'error' || runStatus === 'timeout'
        ? 'failed'
        : 'idle';
  const currentRunId = lifecycle?.processId ?? page?.action.latest_run_id ?? null;
  const currentRunLines =
    view?.items.flatMap((item) =>
      item.kind === 'run' && item.runId === currentRunId ? item.lines : []
    ) ?? [];
  const outcomeShown = currentRunLines.some(
    (line) => line.kind === 'final_output' || line.kind === 'terminal_outcome'
  );
  return {
    kind,
    stopTarget,
    resumable: !lifecycle && page?.action.resumable === true,
    thinking:
      kind === 'running' && !outcomeShown && !currentRunLines.some((line) => line.kind === 'tool'),
    failedWithoutOutcome: kind === 'failed' && !outcomeShown,
    completionEventId: outcomeShown
      ? (page?.runs.find((run) => run.run_id === page.action.latest_run_id)?.completion_event_id ??
        null)
      : null,
  };
}
