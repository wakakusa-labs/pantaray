import { projectActionConversationView } from '../../electron/src/actions/actionConversationModel';
import type { ActionLiveSnapshot } from '../../electron/src/actions/actionLiveCore';

type ToolEntry = ActionLiveSnapshot['transientToolSteps'][number]['entry'];

/** What a running Action is doing now, in the order the History line prefers them. */
export type HistoryLiveStage =
  | { kind: 'tool'; label: string; subject: string | null; outcome: ToolEntry['outcome'] }
  | { kind: 'message'; text: string }
  | { kind: 'thinking' };

const RUNNING_ACTION_STATUSES = new Set(['queued', 'processing']);
// Leading Markdown block markers and inline emphasis would show as raw symbols on one plain line.
const LEADING_BLOCK_MARKER = /^(?:#{1,6}|>|[-*+]|\d+\.)\s+/;
const INLINE_EMPHASIS = /\*\*|__|`/g;

function firstLine(text: string): string {
  const line = text.split('\n').find((candidate) => candidate.trim() !== '') ?? '';
  return line.trim().replace(LEADING_BLOCK_MARKER, '').replace(INLINE_EMPHASIS, '').trim();
}

/**
 * Mirrors the Overlay's busy state: the lifecycle event is authoritative until the canonical
 * page read replaces it, and the page's latest run is the one the Overlay shows as current.
 */
export function selectHistoryLiveStage(snapshot: ActionLiveSnapshot): HistoryLiveStage | null {
  const { lifecycle, page } = snapshot;
  const running = lifecycle
    ? lifecycle.status === 'processing'
    : page !== null && RUNNING_ACTION_STATUSES.has(page.action.status);
  if (!running) return null;

  const runId = lifecycle?.processId ?? page?.action.latest_run_id ?? null;
  const pageRun = page?.runs.find((run) => run.run_id === runId);
  // The row's approval badge already says it is waiting; a line would only repeat it. A page read
  // after a restart says so before the approval blockers arrive over the socket.
  if (snapshot.approvalBlockers.length > 0 || pageRun?.status === 'approval_pending') return null;

  const lines =
    page !== null && pageRun !== undefined
      ? projectActionConversationView([page], [], snapshot.transientToolSteps).items.flatMap(
          (item) => (item.kind === 'run' && item.runId === runId ? item.lines : [])
        )
      : // A run that started after the last page read has only its transient tool steps.
        snapshot.transientToolSteps
          .filter((step) => step.runId === runId)
          .map((step) => ({ kind: 'tool' as const, entry: step.entry }));

  // Lines are oldest first, so the newest match is the last one.
  for (const line of [...lines].reverse()) {
    if (line.kind === 'tool' && line.entry.status === 'processing') {
      const { label, subject, outcome } = line.entry;
      return { kind: 'tool', label, subject, outcome };
    }
  }
  // The message is current only while nothing has run since it; after a finished tool the model
  // is thinking again.
  const latest = [...lines]
    .reverse()
    .find(
      (line) => line.kind === 'tool' || (line.kind === 'assistant' && firstLine(line.text) !== '')
    );
  if (latest?.kind === 'assistant') return { kind: 'message', text: firstLine(latest.text) };
  return { kind: 'thinking' };
}
