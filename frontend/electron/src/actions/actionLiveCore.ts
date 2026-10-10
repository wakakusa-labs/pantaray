import type { ActionConversationPage } from './actionContracts';
import {
  isProcessCompletedActionEvent,
  isProcessStartedActionEvent,
  type OrchestrationServerEvent,
  type ProcessCompletedActionEvent,
  type ProcessStartedActionEvent,
  type ApprovalBlocker,
  type JsonObject,
} from '../orchestration/eventContracts';

type ConversationToolEntry = Extract<
  ActionConversationPage['runs'][number]['entries'][number],
  { step_kind: 'tool' }
>;

export type ActionTransientToolStep = {
  processId: string;
  runId: string;
  entry: ConversationToolEntry & { output_available: false };
};

export type ActionApprovalBlocker = {
  actionId: string;
  processId: string;
  approvalSessionId: string;
  toolRequestId: string;
  toolId: string;
  intentClass: string;
  commandSummary: JsonObject;
};

export type ActionLiveSnapshot = {
  actionId: string;
  page: ActionConversationPage | null;
  // Advances on each canonical read, including changes outside its first page.
  // Transient tool/approval updates retain it across IPC structured cloning.
  pageVersion: number;
  transientToolSteps: readonly ActionTransientToolStep[];
  approvalBlockers: readonly ActionApprovalBlocker[];
  // A lifecycle notification is authoritative while its canonical read is pending or failed.
  lifecycle: {
    processId: string;
    status: 'processing' | ProcessCompletedActionEvent['data']['status'];
  } | null;
};

export type ActionLiveTransientEffect = { kind: 'upsert'; step: ActionTransientToolStep };
type ActionLiveApprovalEffect =
  // rootProcessId is the relay identity of the Action stream, which the snapshot
  // pins to the root process even when a blocker belongs to a subagent.
  | { kind: 'replace'; blockers: readonly ActionApprovalBlocker[]; rootProcessId: string }
  | { kind: 'clear_process'; processId: string }
  | { kind: 'clear_action' };

export type ActionLiveEventRoute = {
  belongsToActionConversation: boolean;
  legacyDisposition: 'pass' | 'suppress';
  actionId: string | null;
  refresh: boolean;
  transientEffect: ActionLiveTransientEffect | null;
  approvalEffect: ActionLiveApprovalEffect | null;
};

export type ActionLiveUpdate =
  | { kind: 'action_updated'; snapshot: ActionLiveSnapshot }
  | { kind: 'reset' };

/** How a canonical read ended: published, failed (and surfaced), or dropped by a reset. */
export type ActionLiveRefreshOutcome = 'refreshed' | 'failed' | 'superseded';

export class ActionLiveIdentityError extends Error {
  constructor(boundary: 'event' | 'page') {
    super(`Action live ${boundary} identity is inconsistent.`);
    this.name = 'ActionLiveIdentityError';
  }
}

const PASS_ROUTE: ActionLiveEventRoute = Object.freeze({
  belongsToActionConversation: false,
  legacyDisposition: 'pass',
  actionId: null,
  refresh: false,
  transientEffect: null,
  approvalEffect: null,
});

function requireMatchingEventIdentity(
  data: { action_id: string; process_id: string },
  meta: { action_id: string; process_id: string }
): void {
  if (data.action_id !== meta.action_id || data.process_id !== meta.process_id) {
    throw new ActionLiveIdentityError('event');
  }
}

function lifecycleRoute(
  event: ProcessStartedActionEvent | ProcessCompletedActionEvent,
  approvalEffect: ActionLiveApprovalEffect
): ActionLiveEventRoute {
  requireMatchingEventIdentity(event.data, event.meta);
  return {
    belongsToActionConversation: true,
    legacyDisposition: 'suppress',
    actionId: event.meta.action_id,
    refresh: true,
    transientEffect: null,
    approvalEffect,
  };
}

function normalizeApprovalBlocker(
  blocker: ApprovalBlocker,
  actionId: string
): ActionApprovalBlocker {
  const identities = [
    blocker.process_id,
    blocker.action_id,
    blocker.approval_session_id,
    blocker.tool_request_id,
    blocker.tool_id,
    blocker.intent_class,
  ];
  if (
    blocker.action_id !== actionId ||
    identities.some((value) => value.length === 0 || value !== value.trim())
  ) {
    throw new ActionLiveIdentityError('event');
  }
  return {
    actionId: blocker.action_id,
    // The blocker's own process, not the root relay identity: the approval
    // decision POST must reach the physical writer that is waiting.
    processId: blocker.process_id,
    approvalSessionId: blocker.approval_session_id,
    toolRequestId: blocker.tool_request_id,
    toolId: blocker.tool_id,
    intentClass: blocker.intent_class,
    commandSummary: blocker.command_summary,
  };
}

export function routeActionLiveEvent(event: OrchestrationServerEvent): ActionLiveEventRoute {
  switch (event.event) {
    case 'process_started':
      return isProcessStartedActionEvent(event)
        ? lifecycleRoute(event, { kind: 'clear_process', processId: event.meta.process_id })
        : PASS_ROUTE;
    case 'process_completed':
      // Only the canonical Action completion reaches this router, and a terminal
      // Action can hold no blocker, so drop every one of them without waiting for
      // the refresh GET that may fail and strand a dead approval button.
      return isProcessCompletedActionEvent(event)
        ? lifecycleRoute(event, { kind: 'clear_action' })
        : PASS_ROUTE;
    case 'process_paused':
      requireMatchingEventIdentity(event.data, event.meta);
      return {
        belongsToActionConversation: true,
        legacyDisposition: 'suppress',
        actionId: event.meta.action_id,
        refresh: true,
        transientEffect: null,
        approvalEffect: {
          kind: 'replace',
          rootProcessId: event.meta.process_id,
          blockers: event.data.approval_blockers.map((blocker) =>
            normalizeApprovalBlocker(blocker, event.meta.action_id)
          ),
        },
      };
    case 'action_step':
      requireMatchingEventIdentity(event.data, event.meta);
      if (
        typeof event.meta.logical_run_id !== 'string' ||
        event.meta.logical_run_id === '' ||
        event.meta.logical_run_id !== event.meta.logical_run_id.trim()
      ) {
        throw new ActionLiveIdentityError('event');
      }
      if (event.data.step_kind === 'assistant') {
        return {
          belongsToActionConversation: true,
          legacyDisposition: 'suppress',
          actionId: event.meta.action_id,
          refresh: true,
          transientEffect: null,
          approvalEffect: null,
        };
      }
      return {
        belongsToActionConversation: true,
        legacyDisposition: 'suppress',
        actionId: event.meta.action_id,
        // A finished Tool is already durable; re-reading the page replaces this
        // transient row with one whose output can be opened, mid-run.
        refresh: event.data.status !== 'processing',
        transientEffect: {
          kind: 'upsert',
          step: {
            processId: event.meta.process_id,
            runId: event.meta.logical_run_id,
            entry: {
              step_kind: 'tool',
              step_id: event.data.step_id,
              step_number: event.data.step_number,
              label: event.data.label,
              status: event.data.status,
              // A step the runtime is still reporting on is doing what it was called for;
              // a denial or an unavailable recorder only shows up in the durable page.
              outcome: 'completed',
              subject: event.data.subject ?? null,
              output_preview: null,
              output_available: false,
              // Attachments and line counts arrive with the durable page.
              images: [],
              file_edit: null,
            },
          },
        },
        approvalEffect: null,
      };
    case 'completion_chunk':
      return {
        belongsToActionConversation: true,
        legacyDisposition: 'suppress',
        actionId: event.meta.action_id,
        refresh: false,
        transientEffect: null,
        approvalEffect: null,
      };
    case 'error':
      if (event.meta?.kind !== 'action') return PASS_ROUTE;
      if (
        typeof event.meta.action_id !== 'string' ||
        event.meta.action_id === '' ||
        event.meta.action_id !== event.meta.action_id.trim()
      ) {
        return PASS_ROUTE;
      }
      return {
        belongsToActionConversation: true,
        // Only running_failed is followed by canonical process_completed; other stages stay legacy-visible.
        legacyDisposition: event.meta.stage === 'running_failed' ? 'suppress' : 'pass',
        actionId: event.meta.action_id,
        refresh: true,
        transientEffect: null,
        approvalEffect: null,
      };
    default:
      return PASS_ROUTE;
  }
}

export function reduceActionLiveSnapshot(
  current: ActionLiveSnapshot | null,
  actionId: string,
  effect: ActionLiveTransientEffect
): ActionLiveSnapshot {
  const steps = current?.transientToolSteps ?? [];
  const index = steps.findIndex(
    (step) => step.runId === effect.step.runId && step.entry.step_id === effect.step.entry.step_id
  );
  const transientToolSteps = [...steps];
  if (index === -1) transientToolSteps.push(effect.step);
  else transientToolSteps[index] = effect.step;
  return {
    actionId,
    page: current?.page ?? null,
    pageVersion: current?.pageVersion ?? 0,
    transientToolSteps,
    approvalBlockers: current?.approvalBlockers ?? [],
    lifecycle: current?.lifecycle ?? null,
  };
}

function findCanonicalToolStep(
  page: ActionConversationPage,
  runId: string,
  stepId: string
): ConversationToolEntry | null {
  const run = page.runs.find((candidate) => candidate.run_id === runId);
  return (
    run?.entries.find(
      (candidate): candidate is ConversationToolEntry =>
        candidate.step_kind === 'tool' && candidate.step_id === stepId
    ) ?? null
  );
}

export function isActionTransientToolStepSuperseded(
  page: ActionConversationPage,
  transient: ActionTransientToolStep
): boolean {
  const canonical = findCanonicalToolStep(page, transient.runId, transient.entry.step_id);
  return Boolean(
    canonical && (canonical.status !== 'processing' || transient.entry.status === 'processing')
  );
}

function transientToolStepKey(step: ActionTransientToolStep): string {
  return JSON.stringify([step.runId, step.entry.step_id]);
}

export function createActionLiveCoordinator(params: {
  readLatestPage: (actionId: string) => Promise<ActionConversationPage>;
  publish: (update: ActionLiveUpdate) => void;
  surfaceRefreshError: (actionId: string, error: unknown) => void;
}): {
  handleEvent: (event: OrchestrationServerEvent) => {
    route: ActionLiveEventRoute;
    refreshDone: Promise<ActionLiveRefreshOutcome> | null;
  };
  refresh: (actionId: string) => Promise<ActionLiveRefreshOutcome>;
  // Returns the Action's root relay process id, which the caller must resume
  // instead of the settled blocker's own process.
  handleApprovalDecisionSettled: (identity: {
    actionId: string;
    processId: string;
    approvalSessionId: string;
    toolRequestId: string;
  }) => string | null;
  handleStreamBoundary: (boundary: {
    kind: 'gap' | 'reconnect';
    actionId: string;
  }) => Promise<ActionLiveRefreshOutcome>;
  getSnapshot: (actionId: string) => ActionLiveSnapshot | null;
  clearAll: () => void;
} {
  const snapshots = new Map<string, ActionLiveSnapshot>();
  // Action id -> root relay process id, so an approval resume can reattach to the
  // root stream even after a reconnect dropped the transport's own registries.
  const rootProcessIds = new Map<string, string>();
  const flights = new Map<string, Promise<ActionLiveRefreshOutcome>>();
  let generation = 0;
  let pageVersion = 0;

  const applyTransientEffect = (actionId: string, effect: ActionLiveTransientEffect): void => {
    const current = snapshots.get(actionId) ?? null;
    if (current?.page && isActionTransientToolStepSuperseded(current.page, effect.step)) {
      return;
    }
    const next = reduceActionLiveSnapshot(current, actionId, effect);
    snapshots.set(actionId, next);
    params.publish({ kind: 'action_updated', snapshot: next });
  };

  const refresh = (actionId: string): Promise<ActionLiveRefreshOutcome> => {
    const existing = flights.get(actionId);
    if (existing) {
      void params.readLatestPage(actionId);
      return existing;
    }
    const startedGeneration = generation;
    const terminalAtReadStart = new Set(
      (snapshots.get(actionId)?.transientToolSteps ?? [])
        .filter((step) => step.entry.status !== 'processing')
        .map(transientToolStepKey)
    );
    const pagePromise = params.readLatestPage(actionId);
    const promise = (async (): Promise<ActionLiveRefreshOutcome> => {
      try {
        const page = await pagePromise;
        if (startedGeneration !== generation) return 'superseded';
        if (page.action.action_id !== actionId) throw new ActionLiveIdentityError('page');
        const current = snapshots.get(actionId);
        const actionIsTerminal = ['success', 'error', 'canceled'].includes(page.action.status);
        const transientToolSteps = actionIsTerminal
          ? []
          : (current?.transientToolSteps ?? []).filter(
              (step) =>
                !terminalAtReadStart.has(transientToolStepKey(step)) &&
                !isActionTransientToolStepSuperseded(page, step)
            );
        // The conversation page carries no blockers, and the latest run status only
        // reflects the root process, so a nonterminal refresh keeps the WS snapshot
        // intact: subagent blockers stay visible while the root run keeps running.
        const snapshot: ActionLiveSnapshot = {
          actionId,
          page,
          pageVersion: ++pageVersion,
          transientToolSteps,
          approvalBlockers: actionIsTerminal ? [] : (current?.approvalBlockers ?? []),
          lifecycle: null,
        };
        snapshots.set(actionId, snapshot);
        params.publish({ kind: 'action_updated', snapshot });
        if (actionIsTerminal) {
          snapshots.delete(actionId);
          rootProcessIds.delete(actionId);
        }
        return 'refreshed';
      } catch (error) {
        if (startedGeneration !== generation) return 'superseded';
        params.surfaceRefreshError(actionId, error);
        return 'failed';
      }
    })().finally(() => {
      if (flights.get(actionId) === promise) flights.delete(actionId);
    });
    flights.set(actionId, promise);
    return promise;
  };

  return {
    handleEvent: (event) => {
      const route = routeActionLiveEvent(event);
      if (route.actionId !== null && route.transientEffect !== null) {
        applyTransientEffect(route.actionId, route.transientEffect);
      }
      if (route.actionId !== null && route.approvalEffect !== null) {
        const current = snapshots.get(route.actionId) ?? null;
        const effect = route.approvalEffect;
        const lifecycle = isProcessStartedActionEvent(event)
          ? { processId: event.data.process_id, status: 'processing' as const }
          : isProcessCompletedActionEvent(event)
            ? { processId: event.data.process_id, status: event.data.status }
            : (current?.lifecycle ?? null);
        if (effect.kind === 'replace') rootProcessIds.set(route.actionId, effect.rootProcessId);
        if (effect.kind === 'clear_action') rootProcessIds.delete(route.actionId);
        const approvalBlockers =
          effect.kind === 'replace'
            ? effect.blockers
            : effect.kind === 'clear_action'
              ? []
              : (current?.approvalBlockers ?? []).filter(
                  (blocker) => blocker.processId !== effect.processId
                );
        const changed =
          effect.kind === 'replace'
            ? (current?.approvalBlockers.length ?? 0) > 0 || approvalBlockers.length > 0
            : approvalBlockers.length !== (current?.approvalBlockers.length ?? 0);
        if (changed || lifecycle !== (current?.lifecycle ?? null)) {
          const snapshot: ActionLiveSnapshot = {
            actionId: route.actionId,
            page: current?.page ?? null,
            pageVersion: current?.pageVersion ?? 0,
            transientToolSteps: current?.transientToolSteps ?? [],
            approvalBlockers,
            lifecycle,
          };
          snapshots.set(route.actionId, snapshot);
          params.publish({ kind: 'action_updated', snapshot });
        }
      }
      return {
        route,
        refreshDone: route.actionId !== null && route.refresh ? refresh(route.actionId) : null,
      };
    },
    refresh,
    handleApprovalDecisionSettled: (identity) => {
      const rootProcessId = rootProcessIds.get(identity.actionId) ?? null;
      const current = snapshots.get(identity.actionId);
      const settlesKnownBlocker = current?.approvalBlockers.some(
        (blocker) =>
          blocker.processId === identity.processId &&
          blocker.approvalSessionId === identity.approvalSessionId &&
          blocker.toolRequestId === identity.toolRequestId
      );
      if (!current || !settlesKnownBlocker) return rootProcessId;
      // A decision releases its whole process back to running, so its remaining
      // blockers are undecidable until the next pause snapshot republishes them.
      const snapshot = {
        ...current,
        approvalBlockers: current.approvalBlockers.filter(
          (blocker) => blocker.processId !== identity.processId
        ),
      };
      snapshots.set(identity.actionId, snapshot);
      params.publish({ kind: 'action_updated', snapshot });
      return rootProcessId;
    },
    handleStreamBoundary: ({ actionId }) => refresh(actionId),
    getSnapshot: (actionId) => snapshots.get(actionId) ?? null,
    clearAll: () => {
      generation += 1;
      snapshots.clear();
      rootProcessIds.clear();
      flights.clear();
      params.publish({ kind: 'reset' });
    },
  };
}
