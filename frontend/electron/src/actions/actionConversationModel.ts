import {
  compareActionTimelinePosition,
  type ActionConversationPage,
  type ActionMessageRequest,
} from './actionContracts';
import {
  isActionTransientToolStepSuperseded,
  type ActionTransientToolStep,
} from './actionLiveCore';

type ConversationRun = ActionConversationPage['runs'][number];
type ConversationEntry = ConversationRun['entries'][number];
type ConversationUserEntry = Extract<ConversationEntry, { step_kind: 'user' }>;
type ConversationToolEntry = Extract<ConversationEntry, { step_kind: 'tool' }>;

export type ActionConversationPageChain = readonly [
  ActionConversationPage,
  ...ActionConversationPage[],
];

export type OptimisticSubmission = Readonly<{
  request: ActionMessageRequest;
  state: 'submitting' | 'awaiting_refresh' | 'failed';
}>;

export type ActionConversationUserItem =
  | Readonly<{
      kind: 'user';
      source: 'canonical';
      key: string;
      visibility: 'always';
      entry: ConversationUserEntry;
    }>
  | Readonly<{
      kind: 'user';
      source: 'optimistic';
      key: string;
      visibility: 'always';
      submission: OptimisticSubmission;
    }>;

export type ActionConversationToolItem = Readonly<{
  kind: 'tool';
  key: string;
  visibility: 'agent_work';
  entry: ConversationToolEntry;
}>;

export type ActionConversationAssistantItem = Readonly<{
  kind: 'assistant';
  key: string;
  visibility: 'always';
  text: string;
}>;

export type ActionConversationOutputLine =
  | Readonly<{
      kind: 'final_output';
      runId: string;
      status: 'success';
      visibility: 'always';
      text: string;
    }>
  | Readonly<{
      kind: 'terminal_outcome';
      runId: string;
      status: 'error' | 'canceled';
      visibility: 'always';
      code: string;
      text: string;
    }>;

export type ActionConversationRunLine =
  | ActionConversationUserItem
  | ActionConversationAssistantItem
  | ActionConversationToolItem
  | ActionConversationOutputLine;

export type ActionConversationRunItem = Readonly<{
  kind: 'run';
  runId: string;
  status: ConversationRun['status'];
  startedAt: string;
  completedAt: string | null;
  lines: readonly ActionConversationRunLine[];
}>;

export type ActionConversationView = Readonly<{
  action: ActionConversationPage['action'] | null;
  items: readonly (ActionConversationRunItem | ActionConversationUserItem)[];
  nextCursor: string | null;
}>;

type RunAccumulator = {
  run: ConversationRun;
  entries: ConversationEntry[];
};

export function applyActionConversationPage(
  current: ActionConversationPageChain | null,
  requestCursor: string | null,
  page: ActionConversationPage
): ActionConversationPageChain {
  if (requestCursor === null) return [page];
  if (current === null) {
    throw new Error('An older Action conversation page requires a current page chain.');
  }
  const tail = current[current.length - 1];
  if (tail.action.action_id !== page.action.action_id) {
    throw new Error('Action conversation pages belong to different Actions.');
  }
  if (tail.next_cursor !== requestCursor) {
    throw new Error('Action conversation page cursor does not extend the current chain.');
  }
  return [...current, page];
}

export function mergeActionConversationLivePage(
  current: ActionConversationPageChain | null,
  page: ActionConversationPage
): ActionConversationPageChain {
  if (current === null || current[0].action.action_id !== page.action.action_id) return [page];
  const headRunIds = new Set(page.runs.map((run) => run.run_id));
  const headStepIds = new Set([
    ...page.runs.flatMap((run) => run.entries.map((entry) => entry.step_id)),
    ...page.unadopted_messages.map((entry) => entry.step_id),
  ]);
  const terminal = !['queued', 'processing'].includes(page.action.status);
  const past = current
    .map((cached) => ({
      ...cached,
      runs: cached.runs.filter((run) => !headRunIds.has(run.run_id)),
      // Terminal Actions cannot still adopt pending messages. Preserve their
      // text without leaving a pending state dependent on a successful read.
      unadopted_messages: cached.unadopted_messages.flatMap((entry) => {
        if (headStepIds.has(entry.step_id)) return [];
        return [
          terminal && entry.status === 'pending'
            ? { ...entry, status: 'not_executed' as const }
            : entry,
        ];
      }),
    }))
    .filter((cached) => cached.runs.length > 0 || cached.unadopted_messages.length > 0);
  return [page, ...past];
}

function canonicalUserItem(entry: ConversationUserEntry): ActionConversationUserItem {
  return {
    kind: 'user',
    source: 'canonical',
    key: entry.message_id === null ? `step:${entry.step_id}` : `message:${entry.message_id}`,
    visibility: 'always',
    entry,
  };
}

function optimisticUserItem(submission: OptimisticSubmission): ActionConversationUserItem {
  return {
    kind: 'user',
    source: 'optimistic',
    key: `message:${submission.request.message.message_id}`,
    visibility: 'always',
    submission,
  };
}

function outputLine(run: ConversationRun): ActionConversationOutputLine | null {
  if (run.status === 'success' && run.final_output !== null) {
    return {
      kind: 'final_output',
      runId: run.run_id,
      status: run.status,
      visibility: 'always',
      text: run.final_output,
    };
  }
  if ((run.status === 'error' || run.status === 'canceled') && run.error !== null) {
    return {
      kind: 'terminal_outcome',
      runId: run.run_id,
      status: run.status,
      visibility: 'always',
      code: run.error.code,
      text: run.error.message,
    };
  }
  return null;
}

function appendCanonicalEntry(
  entries: ConversationEntry[],
  entry: ConversationEntry,
  seenStepIds: Set<string>,
  seenMessageIds: Set<string>
): void {
  let duplicateMessage = false;
  if (entry.step_kind === 'user' && entry.message_id !== null) {
    duplicateMessage = seenMessageIds.has(entry.message_id);
    seenMessageIds.add(entry.message_id);
  }
  if (seenStepIds.has(entry.step_id) || duplicateMessage) return;
  seenStepIds.add(entry.step_id);
  entries.push(entry);
}

function mergeTransientToolStep(
  accumulator: RunAccumulator,
  pages: ActionConversationPageChain,
  transient: ActionTransientToolStep
): void {
  const canonicalIndex = accumulator.entries.findIndex(
    (entry) => entry.step_kind === 'tool' && entry.step_id === transient.entry.step_id
  );
  if (canonicalIndex !== -1) {
    if (pages.some((page) => isActionTransientToolStepSuperseded(page, transient))) return;
    accumulator.entries[canonicalIndex] = transient.entry;
    return;
  }

  const insertionIndex = accumulator.entries.findIndex(
    (entry) =>
      compareActionTimelinePosition(transient.entry, {
        step_number: entry.step_number!,
        step_id: entry.step_id,
      }) < 0
  );
  if (insertionIndex === -1) accumulator.entries.push(transient.entry);
  else accumulator.entries.splice(insertionIndex, 0, transient.entry);
}

function projectRun(accumulator: RunAccumulator): ActionConversationRunItem {
  const outcome = outputLine(accumulator.run);
  // 蓄積順（バックエンドのページ順）は新しい順。表示は古い順に反転し、結果を末尾に置く。
  const entries: ActionConversationRunLine[] = accumulator.entries
    .map(
      (entry): ActionConversationRunLine =>
        entry.step_kind === 'user'
          ? canonicalUserItem(entry)
          : entry.step_kind === 'assistant'
            ? {
                kind: 'assistant',
                key: `step:${entry.step_id}`,
                visibility: 'always',
                text: entry.content,
              }
            : {
                kind: 'tool',
                key: `step:${entry.step_id}`,
                visibility: 'agent_work',
                entry,
              }
    )
    .reverse();
  return {
    kind: 'run',
    runId: accumulator.run.run_id,
    status: accumulator.run.status,
    startedAt: accumulator.run.started_at,
    completedAt: accumulator.run.completed_at,
    lines: outcome === null ? entries : [...entries, outcome],
  };
}

export function projectActionConversationView(
  chain: ActionConversationPageChain | null,
  optimisticSubmissions: readonly OptimisticSubmission[] = [],
  transientToolSteps: readonly ActionTransientToolStep[] = []
): ActionConversationView {
  const runOrder: RunAccumulator[] = [];
  const pendingMessages: ActionConversationUserItem[] = [];
  const runsById = new Map<string, RunAccumulator>();
  const seenStepIds = new Set<string>();
  const seenMessageIds = new Set<string>();

  for (const page of chain ?? []) {
    for (const run of page.runs) {
      let accumulator = runsById.get(run.run_id);
      if (accumulator === undefined) {
        accumulator = { run, entries: [] };
        runsById.set(run.run_id, accumulator);
        runOrder.push(accumulator);
      }
      for (const entry of run.entries) {
        appendCanonicalEntry(accumulator.entries, entry, seenStepIds, seenMessageIds);
      }
    }
    for (const entry of page.unadopted_messages) {
      const projected: ConversationEntry[] = [];
      appendCanonicalEntry(projected, entry, seenStepIds, seenMessageIds);
      if (projected.length === 1) {
        pendingMessages.push(canonicalUserItem(entry));
      }
    }
  }

  if (chain !== null) {
    for (const transient of transientToolSteps) {
      const accumulator = runsById.get(transient.runId);
      if (accumulator !== undefined) mergeTransientToolStep(accumulator, chain, transient);
    }
  }

  const optimisticItems: ActionConversationUserItem[] = [];
  for (const submission of optimisticSubmissions) {
    const messageId = submission.request.message.message_id;
    if (seenMessageIds.has(messageId)) continue;
    seenMessageIds.add(messageId);
    optimisticItems.push(optimisticUserItem(submission));
  }

  // ページ連鎖は「現在の run → 未採用のUSER → 過去の run」の順に、各スコープ内は新しい順で積まれる。
  // 未採用のUSERはどの run よりも後に送られたものなので、スコープごとに古い順へ反転したうえで
  // run 群 → 未採用のUSER → 送信中の順に並べる。
  return {
    action: chain?.[0].action ?? null,
    items: [
      ...runOrder.map(projectRun).reverse(),
      ...pendingMessages.reverse(),
      ...optimisticItems,
    ],
    nextCursor: chain?.[chain.length - 1].next_cursor ?? null,
  };
}
