import { expect, it } from 'vitest';

import type { ActionConversationPage } from '../../electron/src/actions/actionContracts';
import type { ActionLiveSnapshot } from '../../electron/src/actions/actionLiveCore';
import { selectHistoryLiveStage } from './historyLiveStage';

type Run = ActionConversationPage['runs'][number];
type Entry = Run['entries'][number];
type ToolEntry = Extract<Entry, { step_kind: 'tool' }>;

const assistant = (step: number, content: string): Entry => ({
  step_kind: 'assistant',
  step_id: `assistant-${step}`,
  step_number: step,
  content,
});
const tool = (step: number, status: ToolEntry['status'], subject = 'notes.md'): ToolEntry => ({
  step_kind: 'tool',
  step_id: `tool-${step}`,
  step_number: step,
  label: 'read',
  status,
  outcome: 'completed',
  subject,
  output_preview: null,
  output_available: status !== 'processing',
  images: [],
  file_edit: null,
});
/** Entries are given oldest first and stored newest first, as the backend pages them. */
const runningRun = (id: string, entries: Entry[]): Run => ({
  run_id: id,
  status: 'running',
  started_at: '2026-10-02T00:00:00.000000Z',
  completed_at: null,
  completion_event_id: null,
  entries: [...entries].reverse(),
  final_output: null,
  error: null,
});
const page = (
  status: ActionConversationPage['action']['status'],
  runs: Run[]
): ActionConversationPage => ({
  action: {
    action_id: 'A1',
    suggestion_id: null,
    approved_suggestion: null,
    status,
    latest_run_id: runs[0]?.run_id ?? null,
    resumable: false,
  },
  runs,
  unadopted_messages: [],
  next_cursor: null,
});
const snapshot = (overrides: Partial<ActionLiveSnapshot>): ActionLiveSnapshot => ({
  actionId: 'A1',
  page: null,
  pageVersion: 1,
  transientToolSteps: [],
  approvalBlockers: [],
  lifecycle: null,
  ...overrides,
});
const blocker = {
  actionId: 'A1',
  processId: 'R1',
  approvalSessionId: 'session-1',
  toolRequestId: 'request-1',
  toolId: 'bash',
  intentClass: 'write',
  commandSummary: {},
};

it('shows the running tool, else the latest message until a tool finishes after it, else thinking', () => {
  const entries = [assistant(1, '## **Plan**\nread the notes'), tool(2, 'processing')];
  const running = page('processing', [runningRun('R1', entries)]);

  // The approval badge already says the run waits; the line would only repeat it.
  expect(
    selectHistoryLiveStage(snapshot({ page: running, approvalBlockers: [blocker] }))
  ).toBeNull();
  expect(
    selectHistoryLiveStage(
      snapshot({ page: page('processing', [{ ...running.runs[0], status: 'approval_pending' }]) })
    )
  ).toBeNull();
  expect(selectHistoryLiveStage(snapshot({ page: running }))).toEqual({
    kind: 'tool',
    label: 'read',
    subject: 'notes.md',
    outcome: 'completed',
  });
  expect(
    selectHistoryLiveStage(snapshot({ page: page('processing', [runningRun('R1', [entries[0]])]) }))
  ).toEqual({ kind: 'message', text: 'Plan' });
  // Once a tool has finished after the message, the model is thinking again.
  expect(
    selectHistoryLiveStage(
      snapshot({ page: page('processing', [runningRun('R1', [entries[0], tool(2, 'success')])]) })
    )
  ).toEqual({ kind: 'thinking' });
  expect(
    selectHistoryLiveStage(snapshot({ page: page('queued', [runningRun('R1', [])]) }))
  ).toEqual({ kind: 'thinking' });
});

it('shows a transient tool step over the canonical page, and one in a run the page lacks', () => {
  const transient = (runId: string, step: number, subject: string) => ({
    processId: runId,
    runId,
    entry: { ...tool(step, 'processing', subject), output_available: false as const },
  });
  const earlier = page('processing', [runningRun('R1', [assistant(1, 'Looking')])]);
  expect(
    selectHistoryLiveStage(
      snapshot({ page: earlier, transientToolSteps: [transient('R1', 2, 'a.md')] })
    )
  ).toMatchObject({ kind: 'tool', subject: 'a.md' });

  // A follow-up run started before its first page read: the old run's message is not current.
  expect(
    selectHistoryLiveStage(
      snapshot({
        page: { ...earlier, action: { ...earlier.action, status: 'success' } },
        lifecycle: { processId: 'R2', status: 'processing' },
      })
    )
  ).toEqual({ kind: 'thinking' });
  expect(
    selectHistoryLiveStage(
      snapshot({
        lifecycle: { processId: 'R2', status: 'processing' },
        transientToolSteps: [transient('R2', 1, 'b.md')],
      })
    )
  ).toMatchObject({ kind: 'tool', subject: 'b.md' });
});

it('has no stage once the Action stops running', () => {
  const done = page('success', []);
  expect(selectHistoryLiveStage(snapshot({ page: done }))).toBeNull();
  // The completion event is authoritative while its page read is pending.
  expect(
    selectHistoryLiveStage(
      snapshot({
        page: page('processing', [runningRun('R1', [tool(1, 'processing')])]),
        lifecycle: { processId: 'R1', status: 'success' },
      })
    )
  ).toBeNull();
  expect(selectHistoryLiveStage(snapshot({}))).toBeNull();
});
